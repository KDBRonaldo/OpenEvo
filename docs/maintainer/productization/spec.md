# OpenEvo External Beta Product Specification

Status: canonical target and release-acceptance specification; this is not a
statement of current implementation completeness

Tracking issue: #131

Release form: unsigned macOS Desktop application paired with a Linux OpenEvo
Daemon

## 1. Authority And Language

This is the only canonical product specification for the OpenEvo External Beta
target. It defines the product boundary, release-supported workflows,
externally observable behavior, and blocking release evidence.

Architecture documents, module READMEs, issues, tests, and the implementation
plan may refine API, security, storage, and implementation contracts within
this product boundary. They cannot add a product surface or release claim,
remove or weaken a requirement here, or conflict with this document. Current
implementation status and execution sequencing do not belong in this
specification; an unmet requirement is a release blocker rather than evidence
that the target is already supported.

The words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are normative. A release
claim is valid only when the corresponding blocking gate in this document has
passed on the exact release candidate.

## 2. Product Goal

OpenEvo lets a scientist install a macOS application, connect a remote Linux
server, prepare that server without command-line work, run a science task
through a real Codex harness, observe how selected evolution carriers change,
and use the resulting context in the next task.

The release is ready when this workflow works from a packaged Desktop
application against a real remote Daemon in both supported execution modes,
while preserving the behavior and benchmark effectiveness of the three
validated non-parametric evolution methods on their canonical historical
benchmark profiles. Both modes also pass target-specific functional-efficacy
canaries; the release does not infer an unmeasured quantitative benchmark floor
for a different model profile.

OpenEvo is not a general chat client. It is a system for executing agent
workflows, capturing their trajectories, producing typed evolution artifacts,
and activating those artifacts across task sessions.

## 3. Product Model

OpenEvo has exactly two release-facing applications:

1. **OpenEvo Desktop Client** runs on the scientist's Mac. It provides the user
   interface, native operating-system integration, secure local credential
   references, SSH bootstrap, and a private connection to the remote Daemon.
2. **OpenEvo Daemon** runs under the user's account on the remote Linux server.
   It is the backend and the authority for projects, task execution, capture,
   evolution, artifacts, revisions, model services, and managed server state.

```text
macOS
OpenEvo Desktop Client
|- React user interface
|- Tauri native host
`- private Desktop sidecar
       |
       | SSH bootstrap and authenticated private tunnel
       v
Linux server
OpenEvo Daemon (assembled from `src/openevo/` Core)
|- versioned control API
|- project, task, run, and revision owner
|- gateway, rollout, evolution, and worker services
|- Codex harness runtimes
`- managed vLLM deployment when self-deployed
```

`src/openevo/` is the shared Core implementation used by the Daemon. Core is
not a third application that ordinary users install or operate separately. The
Daemon is the deployable application and composition root around Core; it does
not duplicate Core execution or evolution behavior.

The Desktop sidecar is a client-internal transport and state-projection
component. It is not a business backend. It MUST NOT own canonical project,
run, artifact, capability, or revision state.

Benchmark automation is maintainer tooling outside both products. It consumes
Core or Daemon contracts and never becomes a Desktop feature.

The repository dependency direction is:

```text
Desktop Client -> versioned Daemon contracts
OpenEvo Daemon application -> Core implementation
benchmark automation -> Core or Daemon contracts
Core -> neither Desktop nor benchmark automation
```

## 4. User-Facing Concepts

The ordinary-user product uses a small, stable vocabulary:

- **Server**: a remote Linux host running one OpenEvo Daemon for the connected
  operating-system user.
- **Project**: a long-lived research workspace containing project-owned
  execution and evolution configuration, uploaded inputs, tasks, outputs, and
  revision history.
- **Task Draft**: mutable user input that has not been submitted.
- **Task**: an immutable submitted scientific request, including a
  content-addressed task envelope and workspace-input snapshot.
- **Run Attempt**: one execution attempt for a task. Infrastructure retry
  creates another auditable attempt against the same immutable task and
  predecessor project head; it never rewrites an old attempt.
- **Desired Execution Settings**: the project-owned desired mode, model
  deployment reference, and task-network policy. Before task admission, the
  Daemon resolves them into the verified effective execution snapshot of a
  project head.
- **Workspace Snapshot**: an immutable manifest of the project files presented
  to one task or produced by one accepted run attempt.
- **Evolution Target**: a carrier that can change across sessions, such as
  textual memory, skills, or agent-system instructions.
- **Evolution Method**: the selected algorithm that updates one target.
- **Evolution Revision**: an immutable set of evolution artifacts used by one
  or more tasks.
- **Runtime Context Snapshot**: the content-addressed, verified materialization
  of an Evolution Revision for a specific registry and runtime contract. Its
  single identity covers the complete manifest, instructions, staged payloads,
  and integrity digests consumed by a task.
- **Project Head** or **Head Revision**: the workspace snapshot, Evolution
  Revision, Runtime Context Snapshot, and verified effective execution snapshot
  that the next workspace draft is based on. A submitted task pins those exact
  identities. The head advances as one auditable transition and never rewinds.

Wire contracts MUST use distinct opaque identity types for Head Revisions and
Evolution Revisions. A generic `revision` field whose meaning depends on context
is not a stable contract.

Worker leases, registry digests, datasets, materialization handles, internal
job IDs, and service process IDs are diagnostic concepts. They are not primary
navigation or required user knowledge.

## 5. Supported Release Matrix

| Area | External Beta support |
| --- | --- |
| Desktop operating system | Finite Apple Silicon macOS versions enumerated in the release manifest |
| Remote operating system | Finite Ubuntu 22.04/24.04 LTS x86-64 host profiles enumerated in the release manifest |
| Remote harness | Codex |
| Subscription execution | Supported; GPU is not required |
| Self-deployed execution | Supported on a compatible NVIDIA GPU host |
| First subscription release-host profile | `docker_user_container_v1` |
| Evolution targets | Textual memory, skill bundle, and agent system |
| Evolution timing | Cross-session only |
| Desktop package | Unsigned, non-notarized DMG |
| Daemon package | Version-matched self-contained Linux bundle |
| Public CLI, Dev Kit, or PyPI product | Not supported |

The release MAY work outside this matrix, but documentation and UI MUST NOT
claim those environments as supported without adding them to the matrix and
the release gates.

Each manifest lists exact tested operating-system versions. Claiming one macOS
or Ubuntu version does not imply support for a newer point or major release.

The release manifest contains a finite closed clean-host matrix. Each row has a
stable ID and binds an exact macOS build, fresh remote-user/host profile, SSH
authentication form, network/proxy profile, execution mode/model profile,
initial-state assertions, and reset provenance. The rows provide declared
pairwise coverage rather than an implicit full Cartesian product, while every
supported operating system, authentication form, network profile, execution
mode, and model profile appears in at least one row. A candidate cannot omit or
add a row without changing the manifest identity. Each row is assigned to
exactly G3 or G4 and becomes an expected case ID; every row must produce exactly
one indexed, non-simulated result.

Every clean-host row also binds machine-controlled phase deadlines, an overall
deadline, maximum automatic retry counts, interruption/resume accounting, and
the terminal state expected on timeout. An interactive subscription-login wait
uses a separately declared user-action window; once the user completes or
cancels that action, the next machine-controlled deadline resumes. A test
cannot wait indefinitely or hide repeated attempts inside one result record.

The matrix MUST include at least one G3 row and one G4 row using the mandatory
`cn-mainland-restricted-v1` profile. This is a reproducible firewall-like
profile that denies declared direct routes to the Codex service and
release-managed download endpoints, then requires the configured proxy,
Hugging Face endpoint, and container mirror paths to succeed where applicable.
It validates the product's remote proxy/mirror behavior for users in restricted
networks; it does not claim universal availability across every mainland China
carrier, proxy provider, or changing third-party service policy.

The first supported Subscription host profile is
`docker_user_container_v1`. The Daemon runs inside the remote user's Linux
container, the container can use the host Docker Engine API, and it has a
writable bind-mounted data root. The Daemon MUST identify its own container,
pin the exact container and mount identity returned by Docker, keep all runtime
session and staged credential state below the verified container-side data
root, and translate every child-container bind source through the corresponding
Docker-daemon-side root. It MUST verify the resulting child-container mounts
after creation. Missing, ambiguous, changed, read-only, or non-bind mount
evidence blocks readiness. A bare host without the supported Docker API is not
part of this first profile, and OpenEvo never falls back to unsandboxed host
execution. The first profile also requires Docker's default short-container-ID
hostname (`Config.Hostname == Id[:12]`) as the Daemon's initial
self-identification input; preflight MUST verify it rather than accepting an
operator-selected hostname.

Self-Deployed currently requires a release-supported Docker Engine API,
compatible NVIDIA driver, NVIDIA Container Toolkit, sufficient VRAM, and
sufficient model storage. Each release carries versioned host profiles with the
exact container, bind-root, Docker, driver/CUDA, VRAM, memory, disk, and model
constraints enforced by preflight.

The release MUST preserve the established Core execution/evolution data path and
validated evolution algorithms. Productization may reorganize paths, packaging,
imports, adapters, and public interfaces, but it cannot bypass or replace that
architecture. The exact module chain is maintained in architecture
documentation rather than duplicated here.

Runtime and data identity use only:

```text
OPENEVO_*
/openevo/session
.openevo/evolution
openevo.session_completed
```

Legacy product names, public wrappers, runtime markers, and compatibility
surfaces are not part of the release.

## 6. Ordinary-User Workflow

The complete workflow is:

```text
install and open the DMG
-> add a remote server
-> configure optional remote proxy and mirror settings
-> verify the SSH host identity
-> run preflight and install or attach the Daemon
-> create a research project
-> configure project execution settings
-> prepare Codex or the selected self-deployed model
-> upload research inputs
-> configure enabled evolution targets and methods
-> run and monitor a task
-> inspect results, transcript, artifacts, and evolution progress
-> wait for or resolve the next project head
-> run the next task with that committed workspace and evolution revision
```

Every ordinary-user step MUST be available in Desktop. Documentation MUST NOT
require the user to open Terminal, clone the repository, run Python, invoke a
backend launcher, upload a runtime image manually, or choose an internal remote
workspace path.

OpenEvo owns the science runtime image and creates a managed workspace for each
project. The user chooses local files or directories to import. Desktop handles
transfer, integrity verification, and presentation of remote outputs.

Closing Desktop MUST NOT stop a remote run. Reopening Desktop MUST reconnect to
the Daemon, replay missed events, and restore authoritative state.

## 7. OpenEvo Desktop Client

### 7.1 Native And Local Responsibilities

Desktop uses React for the renderer and Tauri for the native application host.
This is a native desktop product architecture, not a hosted website in a
decorative shell.

The Tauri native host owns:

- lifecycle and authentication of the Desktop sidecar;
- the renderer's private Desktop session;
- macOS Keychain access;
- native file and directory selection;
- local notifications;
- safe opening and saving of user-selected local files.

The Desktop sidecar owns:

- non-secret server profiles and local task-text or project-form drafts;
- SSH authentication orchestration and host-key verification;
- Daemon bundle transfer and bootstrap;
- private SSH tunnel establishment and recovery;
- typed projection and forwarding between the renderer and the Daemon.

The renderer communicates only with the authenticated, versioned Desktop-local
API. It MUST NOT receive or control raw SSH commands, Daemon URLs or bearer
tokens, Keychain values, secret-reference resolution, arbitrary host paths, or
sidecar process handles.

The packaged application MUST include everything required by its local
components. It MUST NOT depend on a system Python, Node, Rust toolchain,
development server, source checkout, or globally installed command.

Native-host, sidecar, and renderer communication MUST use private authenticated
sessions over IPC or an equivalent private channel. A release build MUST NOT
expose an unauthenticated localhost API.

### 7.2 Local State

Desktop MAY persist:

- server addresses and user-facing labels;
- accepted SSH host-key records;
- references to Keychain credentials;
- UI preferences;
- non-secret task-text and project-form drafts keyed by server and project;
- the last acknowledged event cursor for each active connection;
- disposable, non-authoritative display caches.

Desktop MUST NOT treat local cache as authority for remote capabilities,
projects, runs, services, artifacts, or revisions. Cache loss or corruption
MUST NOT damage remote state.

Uploaded file bytes and staged workspace manifests are Daemon-owned remote
state, not sidecar drafts. They are content-addressed, recoverable after
Desktop restart, and removed only through an explicit discard, project delete,
or bounded orphan-cleanup operation.

SSH passwords, private-key passphrases, proxy credentials, and Hugging Face
tokens stored on the Mac MUST use Keychain. Plaintext project configuration
contains references, not secret values.

### 7.3 First Run And Server Setup

The first-run flow MUST remain inside Desktop:

```text
server address, SSH identity, and optional remote network settings
-> host-key confirmation
-> remote preflight
-> Daemon installation or attachment
-> first project
-> project execution-mode selection
-> required Codex login or model preparation
-> service readiness
```

Supported SSH authentication methods are:

- SSH agent;
- private key with an optional Keychain-backed passphrase;
- password with a Keychain-backed credential.

A changed host key MUST block connection until the user explicitly reviews and
accepts the new identity. Desktop MUST NOT silently replace a pinned host key.

Remote network settings include:

- HTTP proxy endpoint or host/port;
- HTTPS proxy endpoint or host/port;
- `NO_PROXY`;
- optional proxy authentication;
- optional Hugging Face endpoint;
- optional container registry mirror or release-provided mirror profile.

These values describe the remote server environment, not the Mac network.

### 7.4 Information Architecture

The main application is a work interface, not a marketing page. Its stable
navigation is:

```text
sidebar
|- active server and connection state
|- project list
|- create project
`- System

project workspace
|- Task
|- Evolution
|- Files
`- History
```

When a known server and project are available, Desktop opens the most recent
project. When none exist, it presents the next concrete setup action.

### 7.5 Task Workspace

The Task view provides:

- a task editor;
- input file and directory attachments;
- the project's saved execution settings;
- the project head and evolution revision that the run will use;
- run, cancel attempt, close incomplete task, infrastructure-retry, and
  run-again actions with their distinct task semantics;
- live progress, transcript, tool activity, and result;
- files created or modified by the harness;
- download and local reveal actions.

User-facing task stages are:

```text
Validating
Preparing
Running
Finalizing
Evolving
Waiting for next head
Ready for next task
Needs attention
Cancelled
```

Technical service names and stack traces are hidden by default but available in
an explicit technical-details view.

The user MAY prepare a draft for the next task while a run or evolution is in
progress. During that interval the draft is limited to task text and non-file
form values stored locally by Desktop; remote workspace uploads and mutations
wait for the new head. Submission before that head commits fails with a typed
not-ready response and creates no Task, admission, or run. A failed transition
leaves the draft intact while the user chooses retry, repair, or abandon; it
never falls back to the prior head.

### 7.6 Evolution View

The Evolution view presents one section for each capability-exposed target. For
the release targets, it shows:

```text
Textual Memory
Skills
Agent System
```

Each target provides:

- an enabled toggle;
- a compatible method selector;
- a configuration form generated from the remote method schema;
- the current active artifact;
- the current evolution state;
- content or a safe renderer for the candidate next artifact;
- a previous-versus-next diff where meaningful;
- source task, method, and lineage;
- retry, configuration repair, disable, or explicit abandon actions when a
  transition fails.

Desktop MUST NOT hard-code an execution pipeline between these three targets.
It renders the capability graph supplied by the connected Daemon.

An enabled target with a removed, unknown, or incompatible method remains
visible and blocks a new run until corrected or disabled. Desktop MUST NOT
silently choose another method.

### 7.7 Files And History

The Files view supports:

- file and directory upload;
- progress, cancellation, safe retry, and digest verification;
- replacement/overwrite, rename, move, and delete with explicit confirmation;
- browsing project inputs, task outputs, and evolution artifacts;
- safe preview of supported text, Markdown, JSON, image, and tabular formats;
- single-file download and directory export;
- a research provenance export containing an integrity manifest, project
  configuration, task and attempt metadata, artifact lineage, and user-selected
  research inputs and outputs without secrets or internal service state;
- a complete restorable project backup as defined by the data-lifecycle
  contract.

Internal OpenEvo state is not editable through the file browser.
The release does not include a general code or text editor. Replace, rename,
move, and delete operations create a staged workspace draft against an expected
Project Head; they do not mutate an admitted Task. A head change invalidates an
incompatible staged operation visibly.

The History view is task-oriented:

```text
task input
-> closed admission record
-> run attempts and result
-> transcript
-> evolution transition
-> resulting project head and evolution revision
```

Historical content is read-only. Reusing older workspace or evolution content
requires the Daemon to create a new audited successor of the current project
head; Desktop MUST NOT rewind the head or assemble an unvalidated runtime
context locally.

History lets the user open and export the task input, each attempt and
transcript, workspace input/result snapshots, Evolution Revision, Runtime
Context metadata, artifacts, and lineage. Before restore, Desktop previews the
selected historical workspace, evolution state, or both, identifies inherited
current components, and requires confirmation.

The External Beta provides explicit project backup and restore, not continuous
remote backup. Desktop shows remote project and cache storage use. Server-level
disaster recovery outside those project backups remains the server owner's
responsibility and is documented.

### 7.8 System And Recovery

The System view includes:

- Desktop and Daemon versions and compatibility;
- SSH and tunnel health;
- Codex installation and authentication status;
- managed service health;
- model and image preparation progress;
- GPU, VRAM, memory, disk, and project-storage status;
- retry, doctor, repair, restart, update, and rollback actions where allowed;
- redacted diagnostics export;
- clearly separated actions named **Remove Server From This Mac**, **Delete
  Project From Server**, **Clean Shared Caches**, and **Uninstall OpenEvo
  Daemon**, each with an impact preview.

It is not a developer console. Desktop MUST NOT expose an arbitrary remote
shell, Python console, benchmark runner, worker lease editor, or internal
database tool.

Desktop MUST recover from app restart, tunnel loss, network loss, and stale
local cache without duplicating a mutation. It SHOULD use macOS notifications
for task completion, revision readiness, and actions that require user
attention.

The release UI MUST have usable keyboard navigation, readable contrast,
non-overlapping content, stable controls, and tested layouts across its
supported minimum and common window sizes. Loading, empty, offline, reconnecting,
degraded, failed, cancelled, and successful states are product requirements,
not optional polish.

## 8. OpenEvo Daemon

### 8.1 Host Boundary

One remote operating-system user has one OpenEvo Daemon. Projects are resources
inside that Daemon, not separate daemon installations.

The Daemon runs without root privileges and owns:

- the versioned control API;
- project, task, run, event, and revision state;
- managed workspaces and file transfer state;
- harness execution and capture-mode enforcement;
- trajectory and dataset construction;
- evolution plans, jobs, workers, artifacts, and runtime materialization;
- child-service supervision;
- Codex readiness;
- managed runtime images;
- model download and vLLM lifecycle for Self-Deployed;
- doctor, repair, diagnostics, update, and data-management operations.

The control process MUST remain available when a child service is degraded so
Desktop can inspect and repair the system.

### 8.2 Supported Host Baseline

A supported remote host provides:

- Ubuntu and architecture from the release matrix;
- SSH access to a writable user home;
- enough memory and disk for the selected project execution settings;
- the exact `docker_user_container_v1` profile for the first Subscription
  release: a Docker Engine version and API usable by the remote user plus a
  writable bind-mounted data root whose container-side and
  Docker-daemon-side paths can be proven by the Daemon;
- outbound access or a configured proxy/mirror for required downloads.

Self-Deployed additionally provides:

- a supported NVIDIA GPU and driver;
- NVIDIA Container Toolkit;
- sufficient VRAM for the selected validated model profile.

OpenEvo MUST inspect these prerequisites before large downloads or state
changes. The product MAY assist with explicitly authorized system setup where
safe, but it MUST NOT silently install a kernel or GPU driver, change firewall
policy, alter the Docker daemon, grant privileged group membership, or reboot
the server.

### 8.3 Daemon Bundle

The release ships a version-matched Linux Daemon Bundle containing:

- OpenEvo Core and the Daemon launcher;
- an isolated Python runtime;
- locked Python dependencies;
- the verified evolution registry lock;
- runtime, Codex, and model-service preparation metadata;
- release identity and integrity manifests.

Installing the Daemon MUST NOT require remote PyPI access. Large runtime images,
vLLM images, and model snapshots MAY be downloaded on demand.

Desktop and the Daemon Bundle MUST be tied by one release manifest. Desktop
MUST NOT install an arbitrary latest build or reconstruct a release from a
source checkout.

### 8.4 Installation

Installation is idempotent and recoverable. It exposes preflight, transfer,
verification, candidate startup, readiness, and activation progress. Only an
exact verified immutable generation that passes readiness may become active,
and activation preserves the previous working generation for rollback.

The transfer path MUST work over standard SSH/SFTP and MUST NOT require
`rsync`, Python, or a package manager on the server.

An interrupted or repeated installation resumes safely or restarts from a
clean staging area. An unverified generation never becomes active. A failed
candidate never replaces a working Daemon.

Concurrent Desktop clients, reconnecting installers, and stale upgrade
operations MUST be fenced at the remote-user installation boundary. Their
observable result is one active generation and one Daemon writer; a stale
operation cannot activate after a newer operation has won.

### 8.5 Process And Service Model

The Daemon is the single writer for its managed state and the supervisor for
the Gateway, rollout, evolution, worker, optional vLLM, and managed Codex task
runtime services required by the active projects.

Child services use these externally observable states:

```text
Unavailable
Preparing
Stopped
Starting
Ready
Degraded
Failed
```

The supervisor MUST provide dependency-aware startup, readiness checks, bounded
restart, failure backoff, process-group cleanup, log rotation, redaction, and
resource ownership. Process existence alone is not readiness.

Closing Desktop does not stop the Daemon. The preferred installation uses a
user-level service where the host supports it. A safe detached fallback MAY be
used, but Desktop MUST always be able to ensure or attach the unique Daemon on
reconnection.

At startup the Daemon reconciles interrupted runs, orphan runtimes, stale
leases, staged artifacts, downloads, and partially completed operations.
Recovery MUST preserve committed revisions and MUST NOT permit two live writers
for one state root.

### 8.6 Upgrade And Rollback

Desktop negotiates protocol and release compatibility before mutation.

- A missing Daemon is installed from the Desktop-matched bundle.
- An older compatible Daemon MAY remain attached while active work completes.
- An idle older Daemon MAY be upgraded automatically according to user policy.
- An incompatible older Daemon MUST remain active until its work is quiescent;
  cancelling that work requires a compatible Desktop/Daemon control session,
  not a lifecycle-script shortcut.
- Desktop MUST NOT silently downgrade a newer Daemon.
- An incompatible pairing is read-only where safely possible and otherwise
  blocked with a typed update action.

Upgrade creates an immutable candidate generation, backs up mutable metadata
needed for recovery, performs required migrations, and proves readiness before
activation. Candidate or migration failure restores the previous working
generation and leaves project data, artifacts, model caches, and Codex
authentication intact.

Activation has an explicit rollback barrier. Before the new generation accepts
any incompatible business-state mutation, candidate or migration failure may
atomically restore the old generation and metadata. After the new generation
accepts its first such mutation, including a new Project Head or Runtime Context,
automatic rollback to an older reader is forbidden unless the release profile
proves backward-readable state and lossless reverse migration. Otherwise
recovery proceeds forward with the new generation or a corrected successor; the
retained old bytes are not advertised as an activatable rollback.

Every release generation implements a small manifest-bound maintenance protocol
that remains compatible across the declared upgrade range. It exposes only
release identity, quiescent-or-active status, generation staging, candidate
readiness, activation, and rollback. It cannot inspect or mutate project
business state. This protocol lets Desktop safely defer or perform an upgrade
even when the full control APIs are not mutation-compatible.

### 8.7 Automated Preparation

Within the supported host baseline, OpenEvo prepares:

- the Daemon and user-space dependencies;
- a pinned supported Codex CLI;
- the managed science runtime image;
- the pinned vLLM image for Self-Deployed;
- exact Hugging Face model snapshots;
- user-level service configuration;
- resumable, integrity-checked download caches;
- process-level proxy and endpoint configuration.

Every long operation reports phase, progress when measurable, cancellation
support where safe, retryability, and a typed user action when automation
cannot continue.

OpenEvo MUST estimate required storage before a large model download, publish a
cache entry only after verification, and safely resume interrupted downloads.
Projects MAY reuse verified model and image caches.

### 8.8 Proxy And Restricted Networks

Remote proxy settings apply to remote Codex installation and execution, image
pulls, model downloads, and managed services. Loopback communication among the
Daemon, Gateway, and vLLM is automatically excluded from proxying.

OpenEvo canonicalizes proxy configuration and always merges the required
loopback hosts and addresses into `NO_PROXY`; a user-supplied value cannot
remove those exclusions.

Proxy credentials are secret. Error and diagnostic output MAY identify the
failed endpoint and effective non-secret routing policy but MUST NOT reveal
userinfo, authorization headers, or secret environment values.

Docker-daemon proxy or registry configuration may require administrator
authority. When user-level configuration is insufficient, OpenEvo returns an
exact administrator action instead of repeatedly timing out.

### 8.9 Doctor, Repair, And Diagnostics

Doctor is read-only and checks:

- release and registry identity;
- state and bundle integrity;
- child services and ports;
- Docker Engine capability and the verified user-container bind-root identity;
- Codex installation and authentication;
- GPU and NVIDIA runtime where required;
- image and model availability;
- disk and cache health;
- project and revision consistency.

Repair is scoped and idempotent. It MAY reverify the active generation, restart
managed services, resume downloads, repull corrupted images, reconcile
OpenEvo-owned state, and remove verified orphans. It MUST NOT delete project
data, clear Codex authentication, change system policy, or reset the Daemon
without explicit confirmation.

Diagnostics export is explicit and inspectable. By default it excludes research
inputs, full transcripts, artifact bodies, credentials, and model data.

## 9. Execution And Capture Modes

Every science task is executed by Codex on the remote server. OpenEvo never
bypasses the harness and calls a model API directly to obtain the task answer.

| Dimension | Codex Subscription | Self-Deployed |
| --- | --- | --- |
| Harness | Remote Codex | Remote Codex |
| Model source | Codex subscription | Daemon-managed vLLM |
| Authentication | Codex-managed subscription login | Optional Hugging Face access token |
| Model path | Codex subscription path | Codex -> Core Gateway -> vLLM |
| Required capture | Transcript | Transcript; genuine completion records when available |
| GPU | Optional | Required |
| Release evolution | Three non-parametric targets | Three non-parametric targets |

There is no third arbitrary API-key-and-base-URL execution mode. Desktop MUST
NOT present generic external OpenAI-compatible credentials as an alternative
to a harness.

### 9.1 Codex Subscription

The Daemon installs or verifies a pinned supported Codex CLI. Desktop presents
installation and login status and mediates any interactive login flow without
displaying a generic remote shell.

Subscription is an authentication and execution mode, not a capture mode.
Every subscription run MUST explicitly enable transcript capture. The Daemon
rejects admission when a stable readable transcript path is unavailable.

Subscription trajectories declare that token-level metrics are unavailable.
OpenEvo MUST NOT synthesize token IDs, log probabilities, or loss masks and
MUST NOT claim token-level training support for these runs.

Codex owns its remote subscription credentials. Desktop and the Daemon expose
only authentication status and supported login/logout actions; they do not read
or return the credential.

The raw subscription credential is available only to a dedicated
harness-authentication boundary. It MUST NOT be mounted into the project
workspace, exposed to Codex tool subprocesses, readable by task-authored code,
or available through the task runtime environment. The implementation MAY use
a credential broker, privilege separation, or an equivalent mechanism, but a
read-only credential file in the same trust domain as task code is
insufficient.

### 9.2 Self-Deployed

For a release-supported Self-Deployed project, the user selects a
manifest-bound Hugging Face model profile containing an exact model ID and
commit. The Daemon:

1. verifies the exact model identity and revision from the profile;
2. checks model architecture, storage, VRAM, context, and serving
   compatibility;
3. downloads and verifies the exact snapshot;
4. starts a pinned vLLM deployment;
5. verifies the served identity and readiness;
6. configures Codex to use the Core Gateway and that deployment.

Hugging Face credentials are available only to the Daemon-owned model download
operation. They are not injected into Codex or the task runtime.

The release manifest identifies at least one fully validated model profile,
including exact model revision, vLLM image, serving arguments, hardware floor,
context limit, and compatibility range. Additional model profiles become
release-supported only when they are manifest-bound and pass the same clean-host
and execution gates. An arbitrary unvalidated model ID is not part of the
External Beta contract.

Model deployments are server resources referenced by projects. The Daemon MAY
maintain multiple model configurations, but it MUST serialize or reject
incompatible loading operations according to available GPU resources. It MUST
NOT change model identity during an active run.

Self-Deployed MAY capture completion records through the Core Gateway. Any
token-level capability claim requires the real required fields to be present;
the release non-parametric methods rely on valid text or transcript data.

Parametric memory and adapter training are outside this release. The execution
and revision contracts in this release do not claim adapter loading or training
support.

## 10. Desktop-Daemon Contract

### 10.1 Transport Boundary

Before a healthy compatible Daemon session exists, Desktop uses SSH only for:

- host inspection;
- bundle staging and installation;
- unique Daemon ensure or attachment;
- private tunnel establishment;
- invocation of the manifest-bound maintenance protocol when the control API
  cannot start or is not mutation-compatible.

This SSH lifecycle path may inspect release/quiescence status, ensure, stage,
start, activate, or roll back a Daemon generation. It MUST NOT read or mutate
project, task, run, revision, artifact, database, or model-service business
state.

After the Daemon is healthy and a compatible control session is negotiated, all
project, task, file, service, model, evolution, artifact, and data-management
operations use the versioned Daemon API. Desktop MUST NOT fall back to ad hoc
SSH business commands when an API request fails.

The Daemon binds its API to remote loopback only. Desktop reaches it through an
authenticated private SSH tunnel and verifies the remote release identity.

### 10.2 Required Resource Model

The API provides versioned resources for:

- **System**: protocol, release identity, compatibility, and health;
- **Operations**: long-running preparation, repair, and maintenance progress;
- **Execution and Models**: project-owned mode settings and server-managed
  model deployments;
- **Projects**: saved science configuration;
- **Inputs**: uploaded files and managed workspace manifests;
- **Capabilities**: targets, methods, schemas, defaults, and support reasons;
- **Tasks and Runs**: admission, attempts, cancellation, retry, and results;
- **Events**: replayable timelines and logs;
- **Project Heads**: immutable workspace, Evolution Revision, Runtime Context
  Snapshot, and effective execution composition;
- **Evolution Revisions**: immutable target artifact sets and lineage;
- **Artifacts**: safe metadata, content, renderer payloads, diffs, and lineage;
- **Services**: Codex, Gateway, worker, runtime, and model health;
- **Diagnostics and Data Management**: doctor, repair, export, cleanup, delete,
  and uninstall operations.

Exact routes and payloads belong to the versioned API contract and code. These
resource semantics are product requirements.

### 10.3 API Behavior

Before any mutation, the active tunnel session negotiates and pins:

- API major version and request/response schema identity;
- event schema identity;
- Desktop and Daemon release/build identities and compatibility ranges;
- enabled feature set;
- verified evolution registry identity and availability.

If any pinned identity changes, mutations fail closed until Desktop reconnects
and negotiates a new session. A compatible older Daemon or read-only newer
Daemon is usable only when this predicate explicitly allows the requested
operation.

The contract MUST also provide:

- version negotiation before mutation;
- idempotency keys for mutations;
- asynchronous operation IDs for long work;
- resumable progress observation;
- event cursors and replay after reconnect;
- bounded, paginated logs and collections;
- stable typed error codes;
- retryability and a concrete user or repair action;
- redacted, write-only secret inputs;
- optimistic or explicit concurrency control for saved project changes.

Multiple Desktop clients MAY observe the same Daemon. The Daemon serializes
authoritative mutations, rejects stale expected identities, and never lets a
client's local cache overwrite newer remote state.

The Daemon is the authority. Desktop MUST NOT infer success from SSH process
output, use a local method table, rebuild capabilities from bundled code, or
substitute stale cache when the remote contract fails.

### 10.4 Project Validation And Run Admission

Desktop saves desired project execution and evolution configuration before
submitting a task. The Daemon validates it against server readiness and the
same verified capability registry that will execute it.

When desired execution settings differ from the active head, the Daemon first
resolves and proves a new effective snapshot, validates and materializes active
artifacts against it, and commits a settings-only successor head that inherits
the workspace and Evolution Revision while binding the newly verified Runtime
Context Snapshot. This transition is allowed only when no task or other head
transition is in flight. A task cannot override mode, model deployment, or
network policy; it uses the exact effective snapshot in its predecessor head.

When a verified Daemon upgrade changes the registry or runtime-consumption
contract without changing project settings, a context-rebind successor inherits
the exact workspace, Evolution Revision, and effective execution snapshot while
producing a new Runtime Context Snapshot. Until that successor commits, the
project is visibly not ready for task submission. An artifact no longer accepted
by the new registry blocks rebind with a typed repair action.

Settings-only and context-rebind successors publish no partial state. Failure or
cancellation leaves the prior Project Head and staged workspace draft unchanged
and permits an exact retry. Because both successors inherit the identical
workspace snapshot, successful commit atomically rebinds a staged workspace
draft when its expected workspace identity still matches. Otherwise the draft
is invalidated and must be restaged; a client never guesses that rebinding is
safe.

Admission also verifies that every active evolution artifact is compatible with
the effective harness, model, runtime, and capture settings. An incompatible
artifact or enabled method blocks submission with a typed repair action; it is
never silently omitted.

Each submitted immutable task owns one closed admission record composed of:

```text
predecessor project-head identity
content-addressed project configuration
closed task envelope
workspace-input snapshot
active evolution revision
content-addressed runtime-context snapshot
verified effective execution snapshot for harness, capture, model, runtime,
serving, and task-network policy
verified registry and normalized initial evolution intent
```

Unsaved Desktop draft state, later project edits, service changes, new
artifacts, or model changes MUST NOT alter it. Every infrastructure run attempt
for that task references the same admission record.

Every attempt produces an immutable execution receipt binding the admission
identity to the actual predecessor head, workspace, harness, capture, model,
runtime context, runtime, serving, network-policy, and service-attestation
identities it used. An attempt cannot become authoritative unless the receipt
exactly satisfies the closed admission.

### 10.5 Task, Attempt, Workspace, And Head State

A project has one linear active head. It MAY have one admitted Task or one
successor transition in flight at a time. A next Task Draft may exist locally,
but submission remains not-ready until the transition resolves. Different
projects MAY run concurrently. The release does not expose project-head
branches.

Task and workspace state advance as follows:

```text
mutable task and workspace draft
-> submit
-> immutable task envelope + workspace-input snapshot + predecessor head
-> one or more infrastructure run attempts on that same admission record
-> first authoritative completed attempt
-> immutable workspace-result snapshot + sealed evolution input
-> one successor transition
-> atomically committed project head
```

A run attempt that terminates without a valid authoritative capture MAY be
retried against the same task and predecessor head. Attempts remain separately
auditable. Cancellation or infrastructure failure never fabricates capture or
advances the head.

Cancellation targets the active attempt. If no authoritative completion was
accepted before cancellation, the immutable task remains eligible for an
infrastructure retry or can be explicitly closed. Closing releases the project
for a new task and leaves the predecessor head unchanged. A cancellation racing
with completion is resolved atomically by the Daemon: either the completed
attempt becomes authoritative or the cancellation wins, never both.

Closing an incomplete task preserves its task and attempt audit records but
does not commit the attempt workspace or create evolution input. Cancellation
cannot undo side effects already performed against external scientific services
or network resources; Desktop and user documentation disclose this limitation.

The first attempt that reaches a terminal harness result with valid required
capture becomes authoritative for the task, even when the scientific outcome
is a failure. It closes the task to further attempts and is the only attempt
allowed to produce that task's workspace result and evolution transition.
Running the request again after that point creates a new task based on the
project head that eventually commits.

The authoritative attempt automatically accepts its immutable workspace-result
snapshot for the successor transition. Desktop lets the user inspect and export
that result, but the External Beta does not add a manual pre-commit approval
branch. A user who wants older content later creates an audited restore
successor.

The harness executes in an isolated writable view of the sealed input snapshot.
Its file changes produce an immutable result snapshot. Desktop MAY display and
download those results immediately, but they do not become inputs to another
task until the successor project head commits.

User uploads and file edits between tasks create a Daemon-owned staged
workspace draft with an expected base-head identity. Submission seals that
draft. An admitted Task or successor transition blocks further remote workspace
mutation for the project. If the expected head no longer matches, submission
fails visibly and requires the user to restage against the current head; no
automatic merge or silent data loss is permitted.

One run-result project-head transition combines the accepted workspace-result
snapshot with evolution state and inherits the exact effective execution
snapshot from the predecessor head. When no evolution target is enabled, it
reuses the predecessor's exact Evolution Revision and Runtime Context Snapshot
identities. When evolution is enabled, it commits one new Evolution Revision and
its verified Runtime Context Snapshot after the complete transition. Only a
settings-only successor changes the effective execution snapshot; a
context-rebind successor changes only Runtime Context identity.

Historical restore is allowed only when no task or successor transition is in
flight. It uses an expected-current-head check and creates one atomic audited
successor. Selected historical workspace or evolution content is copied into
that successor; each unselected component is inherited from the current head.
The effective execution snapshot is always inherited. Changing the artifact set
creates a new Evolution Revision and verified Runtime Context Snapshot.
Restore never rewinds or forks the project head and invalidates any local draft
based on the prior head.

Restore validates and stages the complete successor before commit. Failure or
cancellation leaves the current Project Head and draft unchanged and is safely
retryable; only a successful restore invalidates the old-head draft.

## 11. Evolution Framework

### 11.1 Targets And Methods

OpenEvo separates what is evolved from how it is evolved.

- An **Evolution Target** owns one carrier's artifact validation, safe context
  projection, runtime consumption, and presentation contract.
- An **Evolution Method** owns the algorithm that consumes declared inputs and
  produces artifacts for one target.
- An **Evolution Plan** is the immutable resolved selection of enabled targets,
  concrete methods, normalized configuration, the source task's verified
  effective execution snapshot, and verified implementation identities for one
  transition.

The task admission stores normalized initial evolution intent, not a future
plan identity. After authoritative capture and dataset sealing, the Daemon
compiles the initial immutable plan against the task-bound verified registry.
A replacement plan uses that same registry, predecessor head, effective
execution snapshot, authoritative attempt, sealed dataset, and workspace
result; only the explicitly repaired target selections/configuration may
differ. The exact verified implementation identity needed by an unresolved
transition must remain executable for retry, or the user must explicitly repair
or abandon that transition before it can be retired. Whether this is achieved
by retaining a Daemon generation or another verified mechanism is an
architecture decision.

Repair configuration is transition-specific. Desktop MUST ask separately
whether to save the same selection as the project's desired configuration for
later tasks; saving it does not mutate the active or replacement plan.

The release targets are:

| Target ID | User-facing name | Runtime effect |
| --- | --- | --- |
| `text_memory` | Textual Memory | Adds natural-language long-term memory to the next harness context |
| `skill_bundle` | Skills | Adds a validated skill bundle containing `SKILL.md` |
| `agent_system` | Agent System | Adds validated harness instruction content at an allowlisted destination |

Project configuration uses one generic
`evolution.targets.<target_id> = {enabled, method, config}` map. It does not add
target-specific top-level project fields.

Each enabled target stores either one concrete method or a Core-owned selection
resolver exposed for that target. A resolver, such as the supported
agent-system `auto` value, MUST resolve to one concrete method before the
immutable plan and job are created. The plan, lineage, and diagnostics record
both the requested value and concrete result. Disabled targets MAY retain a
draft selection.

A method MAY implement multiple rounds, candidates, evaluators, or internal
model calls through the supported harness, but those choices remain
algorithm-owned and do not become product-layer selection logic.

### 11.2 Verified Registry

The connected Daemon's startup-frozen verified registry is the only authority
for:

- available targets and methods;
- compatibility and support reasons;
- configuration schemas and defaults;
- plan validation;
- worker dispatch;
- artifact and lineage identities;
- Desktop capability rendering.

The capability projection distinguishes:

- methods offered as new user-visible choices;
- accepted methods retained for previously saved compatible configurations;
- Core-owned selection resolvers and their possible concrete methods;
- the configured default and the project-execution-compatible effective
  default.

Desktop MUST preserve a still-accepted saved selection even when it is no
longer offered as a new choice. It MUST NOT present a hidden accepted method as
a new recommendation or conflate a resolver with a concrete method.

Each descriptor carries a stable method/target and implementation identity,
user-facing metadata, a closed schema/defaults, ordered inputs, typed outputs,
support profiles, maturity/exposure, and negotiated config/handler/contribution/
runtime/renderer contract versions. A compiler-owned configuration field is
explicitly declared as Daemon-owned: Desktop does not render it as user input,
and the Daemon replaces stale values from its authoritative project or execution
source before validation. An undeclared field remains user-owned.

The release registry is built from exact verified release distributions.
Desktop does not automatically install arbitrary research plugins, and the
product does not claim that method code is sandboxed from the server user.
Desktop renders only negotiated schema, contribution, and renderer vocabularies.
An unknown contract version remains visible as unsupported and blocks use; it
does not fall back to guessed rendering or execution.

### 11.3 Maintainer Extension Contract

Adding a method for an existing target requires:

1. a method implementation using the supported execution interface;
2. a descriptor and closed configuration schema;
3. method, registry, artifact, dispatch, and integration tests;
4. inclusion of the exact implementation identity in a Daemon release lock.

It MUST NOT require:

- a new Desktop method table;
- a target-specific project field;
- a Gateway branch keyed by method ID;
- a second scheduler or artifact store;
- direct model-API task execution.

After a verified Daemon upgrade, a compatible new method for an existing target
appears through capabilities and Desktop renders it without a Desktop code
change when it uses negotiated contract vocabularies.

Adding a new target is broader. It requires a typed artifact contract, safe
projection and runtime-consumption behavior, presentation metadata, and a
supported safe renderer. Target-specific behavior lives only in verified
descriptors and data-only handlers. When a target fits the generic
contribution and renderer contracts, it can be added without a Desktop release.
Introducing a new runtime-consumption or renderer vocabulary requires explicit
contract negotiation and may require a compatible Core or Desktop release.

### 11.4 Protected Methods

The following methods are behavior-protected:

| Family | Method ID | Historical pass@1 rescue reference |
| --- | --- | ---: |
| Textual memory | `text_memory_expel_reflector` | 12 of 21 |
| Trajectory-to-skill | `skill_bundle_reflector` | 14 of 25 |
| Agent system | `agent_system_gepa_reflector` | 17 of 25 |

Productization MUST NOT change their:

- prompts;
- defaults;
- filtering;
- synthesis or reflection logic;
- candidate generation;
- evaluation, scoring, or tie-breaking;
- best-result selection;
- artifact semantics.

For the agent-system method, candidate evaluation and selection performed
inside the algorithm are valid protected behavior. Core records and validates
the algorithm's result; it does not rerank candidates or substitute another
result.

Paths, imports, packaging, adapters, descriptors, and infrastructure MAY
change when behavior guards and performance gates continue to pass.

The release owns a canonical protected baseline tied to the
pre-productization implementation. It combines behavior fixtures and
performance gates with source/resource digest checks for known prompts,
defaults, filters, evaluators, selection rules, and artifact construction.
Source checks are a defense against accidental edits, not a claim to prove all
transitive behavior by hashing.

The machine-readable source comparison permits only reviewed file/module
relocation, corresponding import-path rewrites, and framework adapter or
descriptor code outside protected algorithm bodies. Any unexplained protected
source/resource difference blocks release even when a stochastic benchmark
floor happens to pass.

### 11.5 Cross-Session Transition

Every evolution method, including parametric methods when supported, takes
effect only across sessions:

```text
run N is admitted on project head H containing workspace W, evolution
revision R, runtime context C, and effective execution snapshot E
-> Codex executes entirely on W, C, and E
-> transcript or trajectory is captured
-> the transition dataset is sealed
-> enabled methods run outside active inference
-> all outputs validate and materialize
-> successor project head H+1 containing W+1, R+1, verified C+1, and inherited
E commits
-> the next run is admitted on H+1
```

A method never mutates the context, model, adapter, or filesystem injection of
an active run.

A completed run with a valid transcript MAY produce evolution input regardless
of whether the scientific task succeeded. An interrupted run without valid
capture does not produce a fabricated dataset.

Independent targets MAY execute concurrently. The successor is still one
atomic transition:

- every enabled target must complete successfully;
- disabled or unchanged targets inherit their prior committed artifact;
- outputs remain staged and unavailable to runtime consumption until the
  transition commits;
- one failed target prevents partial activation;
- the prior Project Head, Evolution Revision, and Runtime Context Snapshot remain
  committed and recoverable.

While a successor is pending, the next Task Draft remains visibly not ready and
cannot be submitted. It MUST NOT silently run on the prior Project Head. After a
failed transition:

- retrying transient execution preserves the same immutable plan and records a
  new transition attempt;
- changing method configuration or disabling a target creates a new immutable
  plan bound to the same predecessor, authoritative run, sealed dataset, and
  workspace-result snapshot;
- explicitly abandoning evolution creates a successor project head containing
  the accepted workspace result and the predecessor's exact Evolution Revision
  and Runtime Context Snapshot.

Creating a replacement plan is one atomic compare-and-set operation against the
expected current head and transition generation. It makes every prior plan for
that transition ineligible to commit before the replacement becomes visible;
an old retry cannot race ahead of the user's new choice.

Only one transition attempt can commit the successor of a project head.
Commit and abandon both compare-and-set the expected predecessor while
advancing the head and invalidating every remaining candidate in the same
atomic operation. Failed and superseded outputs remain non-active and are
cleaned according to the artifact lifecycle; they never become context through
fallback.

### 11.6 Artifacts And Lineage

Evolution outputs are immutable typed artifacts. Each active contribution
records enough lineage to identify:

```text
source project, task, run, and sealed dataset
source revision and prior artifact
target and concrete method
normalized method configuration identity
verified implementation identity
execution and capture profile
compatibility and integrity
algorithm-provided scores when present
```

Core validates, stores, materializes, and injects method outputs. It does not
reinterpret algorithm-specific candidate policy.

Artifact payloads and renderers are treated as untrusted data. They cannot
execute plugin HTML or JavaScript, fetch remote preview content automatically,
escape managed roots, override reserved environment variables, or inject
arbitrary host paths.

## 12. Benchmark Automation And Performance

Benchmark-specific code is not Core, Daemon, or Desktop product code. Each
benchmark is a standalone automation package under `benchmarks/` or in an
external benchmark repository.

A benchmark automation owns:

- benchmark task acquisition;
- benchmark-specific runtime and verifier integration;
- task lists and evaluation configuration;
- benchmark reports and evidence.

Across releases, benchmark automation consumes the versioned Daemon API. A
same-candidate automation package MAY use exact-version in-process Core
interfaces for lower-level tests, but that maintainer ABI is not a separately
distributed product or compatibility promise.

Terminal Bench, scientific benchmarks, and general-agent benchmarks use sibling
automation packages. Desktop exposes no benchmark controls.

Release performance evidence MUST exercise the real data path through capture,
dataset sealing, verified method dispatch, artifact registration,
materialization, revision activation, runtime injection, and the authoritative
follow-up harness attempt through the same candidate's supported control path.
Directly calling a method function is unit-test evidence, not release
performance evidence.

### 12.1 Performance Manifests

The repository MUST contain canonical, reviewable manifests for the three
historical baseline-failed subsets. Their closed schema belongs to benchmark
automation, but each manifest pins:

- benchmark, task IDs, runtime image, harness, model, and evaluator;
- method configuration and algorithm candidate/evaluator budget;
- protected baseline and behavior-fixture identities;
- initial workspace, Project Head, Evolution Revision, Runtime Context Snapshot,
  and prior artifact set;
- source attempt, capture, sealed dataset, and construction identities;
- one candidate gate reservation and complete append-only task-launch ledger
  identity covering the baseline and evolved arms;
- one authoritative result format and a closed infrastructure-replacement
  policy.

These historical preservation profiles use the execution mode and model
identity recorded by the canonical baseline, currently Codex Subscription.
Their floors protect the previously demonstrated method behavior; they are not
silently transferred to a different Self-Deployed model.

Each method runs independently with no artifact from another target. Every task
has exactly one authoritative same-candidate no-artifact baseline pass@1 attempt
and exactly one authoritative evolved pass@1 attempt. Both arms pin the same
task, initial workspace, effective execution snapshot, mode/model, runtime,
evaluator, and non-target artifact set. Only a baseline score of `0` followed
by an evolved score of `1` counts as one rescue. Baseline successes and evolved
failures remain in the complete report and cannot be discarded or rerun to
improve the aggregate.

Benchmark automation instantiates independent source/control and treatment
projects from the same immutable canonical genesis; it does not fork either
project's linear Head. The control project's authoritative baseline capture and
sealed dataset are the method's source input. Only that content-addressed
dataset and its provenance may cross into the treatment project through the
versioned benchmark/Core input contract. Workspace results, service state,
artifacts, runtime state, and Project Head state cannot cross.

The treatment project starts with the exact canonical workspace and empty
target artifact state, runs the protected method through verified dispatch,
activates the result through its normal successor Project Head, and launches
the evolved evaluation in a fresh benchmark runtime. The manifest and receipts
prove that the two projects share the task, genesis workspace, effective
execution snapshot, mode/model, runtime, evaluator, and non-target artifacts,
and that only the sealed source dataset and resulting target contribution
distinguish the treatment path.

The evolved attempt runs only after the protected algorithm has completed its
internal candidate evaluation, selected its result, and the result has been
activated in the successor revision. Algorithm-internal candidate trials are
not gate attempts and cannot be counted as rescues.

The gate profile sets `n_attempts=1` independently for the authoritative
baseline and evolved arms. Evidence labels algorithm evaluation records
separately from `authoritative_baseline_score`,
`authoritative_evolved_score`, and `authoritative_rescue`.

An infrastructure failure that prevents scoring MAY be rerun when the failure
and replacement are recorded. A scored failure cannot be rerun merely to obtain
a better sample, and release evidence cannot select the best of repeated
candidate release runs.

Historical references and blocking stochastic floors are:

| Method | Historical reference | Blocking floor |
| --- | ---: | ---: |
| `text_memory_expel_reflector` | 12/21 | 10/21 |
| `skill_bundle_reflector` | 14/25 | 12/25 |
| `agent_system_gepa_reflector` | 17/25 | 15/25 |

Missing a floor blocks release and requires root-cause analysis and a new
declared candidate run after a relevant correction. The floor does not permit
intentional algorithm changes or unexplained systematic regression.

Gate profiles record both `historical_reference` and `blocking_floor`. Unless a
profile explicitly pins a prior artifact, the complete initial artifact set is
empty. The aggregator fails closed on an extra scored attempt, an unclassified
replacement, a launch or result not reachable from the one reserved ledger, or
an unindexed repeated candidate gate run. Historical-summary-only manifests are
not executable release evidence.

## 13. Security, Privacy, And Data

### 13.1 Connection And Process Isolation

- Desktop verifies and pins SSH host identity.
- The Daemon API binds only to remote loopback.
- Desktop reaches it only through an authenticated private tunnel.
- Daemon bearer material is private, permission-restricted, and rotated on a
  safe lifecycle boundary.
- Native-host, sidecar, and renderer communication is private and
  authenticated.
- Task runtimes are unprivileged.
- Task runtimes do not receive SSH keys, the Docker socket, the Daemon state
  root, the complete user home, raw subscription or Hugging Face credentials,
  or arbitrary host mounts.
- Runtime mounts are limited to the project workspace and the Runtime Context
  Snapshot.
- Paths and deletion operations enforce containment and no-follow behavior.

Science tasks MAY need outbound network access for literature search, data
retrieval, or package use. The effective project execution settings and
documentation MUST make this clear. Network access does not imply access to
Desktop-local files or credentials.

Outbound task-network access is a project-owned setting, enabled by default for
science workflows and disableable before task submission. Admission pins the
effective policy; an active task cannot change it.

### 13.2 Secrets

Secrets include SSH credentials, proxy credentials, Hugging Face tokens,
Codex authentication, authorization headers, and Desktop-Daemon session
material.

Secret values are:

- stored in Keychain on the Mac when retained locally;
- stored in permission-restricted remote secret state only when required;
- accepted through write-only API fields;
- excluded from project configuration returned to the renderer;
- redacted from logs, events, errors, transcripts, datasets, artifacts,
  lineage, diagnostics, benchmark evidence, and release files.

Redaction MUST be tested with a closed source-by-sink matrix and distinct
canary values for Keychain, remote secret store, configuration, environment,
harness authentication/input, service response, database write, artifact,
diagnostics, backup, and export boundaries.

Secret filtering MUST NOT silently rewrite ordinary scientific inputs merely
because they resemble sensitive text. Diagnostics and export flows still
require explicit user review for research-data privacy.

### 13.3 Data Authority And Retention

The Daemon is authoritative for:

- project configuration;
- uploaded inputs and managed workspaces;
- task, run, transcript, event, and result history;
- evolution datasets, artifacts, lineage, and revisions;
- managed service, operation, model, image, and cache state.

Project-owned configuration, workspace snapshots, task/admission/attempt
history, transcripts, datasets, Evolution Revisions, Runtime Context Snapshots,
artifacts, lineage, and Head Revisions are retained until the user deletes the
project. They are never removed automatically for capacity pressure.

Redacted service logs are size/time rotated according to a release-manifest
policy; task transcripts and timelines are not service logs and remain
project-owned history. Failed staging, orphan runtime, and incomplete download
data are cleaned only after recovery has classified them as unreachable.
Unreferenced model, image, and download caches MAY be evicted and later
re-created. When capacity remains insufficient, the Daemon blocks new
space-consuming operations and presents cleanup choices instead of deleting
authoritative project data.

A **project backup** is a Daemon-created, versioned, integrity-verified archive
of all project-owned authoritative metadata and payloads required to restore the
project. It excludes credentials, service secrets, and shared model/image
caches. Backup requires the project to have no Task or transition in flight and
uses one consistent Head boundary. Desktop warns that the archive contains
research data and saves it only to a user-selected destination.

Restore verifies the complete archive before mutation and imports it as a new
project identity while preserving source identities and lineage in provenance.
Only releases within the archive's declared compatibility range may restore it.
Failure exposes no partial project. The release gates exercise backup,
verification, corruption rejection, and restore.

**Remove Server From This Mac** removes the local profile, accepted host-key
record, and selected Keychain references only. It does not delete the remote
Daemon or research data.

Deleting a project is one idempotent Daemon-owned asynchronous operation. It
first prevents new project work, then stops or waits for conflicting work,
removes the project's managed workspace, history, datasets, artifacts, and
revision metadata across every owning store, and verifies the final state
before reporting success. A partial delete remains visible and safely
retryable. Shared verified model and image caches are not deleted with one
project.

**Uninstall OpenEvo Daemon** offers distinct, explicitly confirmed choices:

- remove the Daemon and retain projects and caches;
- remove the Daemon and projects but retain shared model caches;
- remove all OpenEvo-managed data.

**Clean Shared Caches**, **Delete Project From Server**, Desktop application
removal, local server-profile removal, and Daemon uninstall are separate
operations with separate impact previews.
OpenEvo does not claim that normal filesystem deletion is physical secure
erasure.

### 13.4 Telemetry And Data Disclosure

Analytics, crash upload, telemetry, and diagnostics upload are disabled by
default.

Diagnostics are generated locally and exported only by explicit user action.
Research inputs, full transcripts, artifact bodies, and secrets are excluded
by default.

Documentation and the project execution UI disclose that Subscription mode
sends task content through the selected Codex subscription service.
Self-Deployed inference remains on the remote server except for user-requested
network activity, model/image downloads, and other explicitly configured
external resources.

## 14. Repository, Distribution, And Documentation

### 14.1 Repository Boundary

The active repository presents:

```text
src/openevo/   Core implementation and Daemon backend
desktop/       macOS Desktop Client
benchmarks/    standalone benchmark automation
docs/          user, operator, maintainer, and architecture documentation
```

Command-line entrypoints may exist only as Daemon launchers, maintenance
tools, CI tools, or benchmark automation. They are not advertised as a public
CLI or Dev Kit product.

Active source, metadata, examples, workflows, screenshots, and documentation
use current OpenEvo names and paths. Historical records MAY remain in clearly
marked maintainer history locations but are not part of user navigation or
current instructions.

### 14.2 Release Artifacts

One GitHub Release contains:

- the Apple Silicon OpenEvo Desktop DMG;
- the exact Linux x86-64 OpenEvo Daemon Bundle;
- a machine-readable release manifest;
- SHA-256 checksums;
- the source tag or archive;
- release notes;
- supported-environment and known-limitation statements;
- complete dependency, license, provenance, and applicable vulnerability
  evidence for shipped and release-managed downloaded components;
- a closed prepublication evidence bundle containing G1-G11 records and its
  machine-readable inventory.

The release manifest binds:

```text
Desktop version and architecture
Daemon and protocol version
request/response and event schema identities
feature compatibility range
Core and verified registry identity
Codex CLI version, source, integrity, and license identity
managed science runtime image digest
verified release-host profile schema and closed constraints
vLLM image digest
validated self-deployed model profile
validated remote-host profile
supported platform matrix
closed clean-host matrix row identities
packaged-app ready deadline and tested window matrix
retention, log-rotation, and cache-eviction policy
gate-profile and evidence-schema digests
artifact checksums
```

The static release manifest does not bind an observed host path, container ID,
mount ID, bind-root inode, or session inode. Those identities exist only after
deployment and MUST be captured and pinned by runtime preflight and admission,
then carried by the applicable execution receipt. The receipt binds the
observed identities to the manifest-bound profile constraints and release
identity.

No manifest, checksum inventory, evidence index, or attestation includes its
own digest. An outer inventory may bind an inner object; self-referential hash
contracts are forbidden.

The DMG contains the matched Daemon Bundle or obtains only the exact
manifest-bound bytes through a verified download. Remote bootstrap verifies the
bundle before installation.

PyPI is not an External Beta release surface. Editable source installation is a
maintainer workflow and MUST NOT be documented as Desktop or Daemon
installation.

The DMG has no Developer ID signature and is non-notarized for this release.
The contained app bundle MUST be ad-hoc signed as one internally consistent
bundle. Candidate validation MUST apply a synthetic browser quarantine to the
copied app, execute the documented recursive quarantine-removal command,
revalidate the complete app signature, and launch those exact bytes. User
documentation provides that concise manual launch procedure without implying
Developer ID signing or notarization.

Release security evidence covers the DMG, Daemon Bundle, Codex CLI, managed
runtime and vLLM images, model profile, and their reachable dependencies.
Applicable vulnerability data is refreshed within seven days of candidate
evaluation. No unresolved known Critical or High vulnerability may affect a
reachable component. No shipped or release-managed component may have an
unknown, incompatible, or prohibited license. A Medium-or-lower vulnerability
exception requires a linked issue, affected-component analysis, owner, expiry,
and release-note disclosure; an exception is part of the candidate evidence
index.

Release construction follows one non-circular DAG:

1. Freeze the source commit, protected release-policy baseline, closed gate
   profiles, and evidence schemas.
2. Build and freeze the DMG, Daemon Bundle, release manifest, checksums, release
   notes, and managed-component identities.
3. Run G1-G11 against those exact frozen bytes and identities.
4. Create the prepublication evidence bundle and index for G1-G11, including
   the exact expected G12 case IDs and procedure but no fabricated G12 verdict.
5. Upload the immutable release payload and prepublication evidence bundle to a
   non-public draft or staging release with no replacement.
6. Download the complete declared draft asset set into a clean environment,
   revalidate its bytes and metadata, and emit a detached G12 attestation.
7. Create one detached final candidate evidence index that binds the
   prepublication index, G1-G11 reports, G12 attestation, and final G1-G12
   verdicts.
8. After all gates have ended, the publication controller validates that final
   index as the publish-eligibility record, then publishes only by changing
   visibility; no tag, manifest, note, or asset byte may change.

The detached G12 attestation and final candidate evidence index are protected
publication records, not members of the draft asset set they attest. Their
storage identity and digest are retained with the publication audit record.
This exclusion is explicit and prevents either object from needing to validate
itself. Any payload or evidence change creates a new candidate and reruns the
affected gates.

After publication, automation records a non-gating publication attestation that
the public tag and downloadable bytes still match the G12-eligible draft. A
mismatch marks the release invalid immediately: distribution links are removed
or the release is withdrawn, users are notified, and a corrected build uses a
new candidate identity. This post-publication attestation is not part of the
pre-publication G1-G12 dependency cycle.

### 14.3 Required Documentation

Release-facing documentation covers:

- product overview and Desktop/Daemon boundary;
- supported Mac and server environments;
- DMG installation and unsigned launch;
- server setup, SSH identity, proxy, and troubleshooting;
- Subscription login and data disclosure;
- Self-Deployed model preparation and hardware requirements;
- project, task, files, evolution, history, and recovery workflows;
- diagnostics, retention, project backup/restore, server-owner disaster
  recovery responsibility, deletion, cache cleanup, and uninstall;
- Core and Daemon architecture;
- method and target integration for maintainers;
- standalone benchmark automation;
- release construction and validation;
- privacy, security, limitations, and typed error guidance.

Documentation MUST describe actual release behavior. Planned or unavailable
features are clearly marked and cannot be used to satisfy a release claim.

## 15. Blocking Release Gates

All gates are blocking. They run against the same identified release candidate
commit and manifest-bound artifacts. A prose signoff cannot replace failed
executable or observed evidence.

Evidence records the environment, input identity, command or UI path, output,
and pass/fail result. Infrastructure failures are distinguished from product
failures and cannot be used to discard a valid negative result.

The detached final candidate evidence index binds the release manifest digest,
candidate commit, artifact hashes, prepublication evidence index, detached G12
attestation, environment identities, report hashes, CI or operator-run
references, `simulator=false` for every release gate, and the final status of
G1 through G12. For every gate it also binds the closed profile/schema digest,
exact expected case IDs, exact result record set, and one aggregate verdict.
The verifier rejects a missing, extra, duplicated, unindexed, or conflicting
case or run, even when an aggregate report says `pass`. The release publisher
revalidates all referenced evidence and refuses publication when any gate is
missing, pending, simulated, or bound to another candidate.

The final index is generated only after G12 has produced its independent
attestation. It records the G12 verdict but is not an input, pass criterion, or
piece of evidence for G12. Its validation is the publication controller's
post-gate eligibility check.

Closed gate profiles derive from a protected release-policy baseline frozen
before candidate evaluation and identified independently of the candidate
manifest. Stable required case IDs cannot be removed; deadlines cannot be
lengthened, accepted cardinalities widened, or expected safety states weakened
without an explicit canonical-spec and release-policy change reviewed before a
new candidate freeze. The verifier performs this monotonic comparison for every
gate with a closed profile, including the clean-host matrix and G8-G11, before
candidate evaluation.

### G1. Product And Contract Boundary

Pass criteria:

- only Desktop and Daemon are presented as applications;
- Core has no dependency on Desktop or benchmark packages;
- benchmark-specific code is outside Core and Desktop;
- no public CLI, Dev Kit, PyPI install path, or legacy product identity is
  advertised;
- packaged Desktop uses only authenticated versioned Daemon capabilities and
  APIs after bootstrap;
- session compatibility pins the required release, schema, feature, and registry
  identities before mutation;
- no local method table, release fallback backend, simulator, or ad hoc SSH
  business path is active;
- capability conformance covers visible choices, retained accepted choices and
  their lossless round-trip, resolver-to-concrete identities, configured and
  effective defaults including no effective default, independent
  execution/capture/harness/runtime support reasons, and unsupported contract
  versions;
- Daemon-owned configuration injection removes stale user values and supplies
  the verified harness/provider identity in both execution modes;
- a fixture method for an existing target appears and is configurable without a
  Desktop code change when it uses the negotiated generic contracts;
- a fixture target crosses capability, planning, artifact, Runtime Context, and
  Desktop rendering through existing negotiated vocabularies without a Desktop
  code change, while an unknown vocabulary fails closed.

Evidence: repository checks, package/import tests, release-content inspection,
contract and capability conformance suites, generic-extension fixtures, and
documentation link checks.

### G2. Packaged Desktop Installation

Environment: a new user account on every exact macOS build claimed by the
release manifest, with no repository checkout, Python, Node, Rust, application
install, OpenEvo support directory, OpenEvo Keychain item, or accepted host key.
The manifest declares a product-ready deadline no greater than 120 seconds on
the gate machine.

Pass criteria:

- the DMG mounts, installs, and launches through the documented unsigned-app
  flow;
- the bundled native host and Desktop sidecar start without a terminal;
- the renderer reaches the real product shell within the declared deadline;
- first-run, quit, relaunch, removal, and reinstall work;
- removing Desktop or a local server profile does not delete remote Daemon or
  project state; an explicit local-state cleanup removes only documented local
  files and Keychain references;
- no development server, simulator, or source-relative fallback is used.

Evidence: packaged-app smoke logs, screenshots, artifact checksum, and tested
machine identity.

### G3. Clean Subscription Deployment

Environment: each claimed subscription-capable Linux host profile, with SSH
to a fresh `docker_user_container_v1` user container. The container can use the
supported host Docker Engine and has an empty writable bind-mounted data root,
but has no usable NVIDIA GPU, OpenEvo, Python, `rsync`, Codex, or managed
runtime image.

Pass criteria:

- every manifest row assigned to G3 produces exactly one complete result;
- every row stays within its declared phase/overall deadlines and retry
  cardinalities, with user-login waiting accounted separately;
- Desktop installs and attaches the exact Daemon Bundle;
- the Daemon verifies and pins its Docker bind-root mapping before run
  admission;
- Codex and the managed runtime image are prepared through Desktop;
- the user completes the mediated subscription login;
- one real no-evolution task runs through Codex with transcript capture and an
  immutable matching execution receipt;
- Desktop disconnect and reconnect preserve the run and timeline;
- across the clean-host matrix, SSH agent, private-key/passphrase, password,
  host-key change rejection, unauthenticated proxy, and authenticated proxy
  paths each receive at least one end-to-end bootstrap test;
- the required `cn-mainland-restricted-v1` row proves direct-route denial and
  successful proxied Codex preparation, login, execution, and applicable
  release-managed downloads;
- retained local credentials use Keychain references, and secret canaries are
  absent from renderer state, project configuration, logs, and diagnostics.

Evidence: preflight report, installation operation history, service health,
authentication/bootstrap matrix, task/run record, transcript-capture
declaration, execution receipt, and packaged Desktop trace.

### G4. Clean Self-Deployed Deployment

Environment: every manifest-claimed self-deployed host/model profile, with the
required driver, Docker Engine, and NVIDIA runtime but no OpenEvo, Python,
`rsync`, Codex, managed science runtime image, vLLM image, or model cache.

Pass criteria:

- every manifest row assigned to G4 produces exactly one complete result;
- every row stays within its declared phase/overall deadlines and retry
  cardinalities;
- Desktop installs the Daemon;
- Codex, the managed science runtime image, the exact reference model, and the
  vLLM image are prepared and verified through Desktop;
- serving readiness proves the expected model identity;
- Codex executes one real no-evolution task through Core Gateway and vLLM with
  an immutable matching execution receipt;
- interruption and resume of at least one model or image download are
  demonstrated, and the resumed bytes have the manifest-bound digest;
- the required `cn-mainland-restricted-v1` row proves direct-route denial and
  successful configured remote proxy, Hugging Face endpoint, and container
  mirror routing for required downloads while loopback Daemon/Gateway/vLLM
  traffic remains excluded.

Evidence: hardware/preflight report, model snapshot identity, image digest,
service readiness, run snapshot, execution receipt, download-resume record, and
packaged Desktop trace.

### G5. Mode-By-Target Evolution

Matrix: both execution modes multiplied by each of the three release targets,
with one target enabled per test.

Pass criteria for each of six paths:

- a closed machine-readable canary profile pins the mode, target, task,
  environment, baseline state, artifact state, and deterministic pass predicate;
- exactly one authoritative no-artifact baseline attempt scores `0/1`;
- run N produces valid capture and a sealed transition input;
- the canary generation task produces no workspace-content delta, so the
  baseline and follow-up use the same immutable evaluation task, Workspace
  Snapshot, effective execution snapshot, mode/model, and non-target artifact
  set;
- the selected protected method runs through verified dispatch;
- its typed artifact validates and materializes;
- successor Project Head H+1 atomically binds Evolution Revision R+1 and Runtime
  Context Snapshot C+1;
- run N+1 is admitted on H+1;
- runtime evidence proves that Codex consumed the artifact and the follow-up
  canary scores `1/1`;
- the target artifact contribution is the only controlled difference between
  the baseline and exactly one authoritative follow-up attempt;
- method evidence proves the protected reflector used the verified
  harness/provider path for that execution mode rather than a user-supplied
  model endpoint.

Evidence: six machine-readable transition reports plus corresponding task,
baseline-canary, artifact, project-head, revision, and follow-up efficacy
records.

### G6. Atomic Combined Evolution

Pass criteria:

- all three targets can evolve from one completed run;
- one successor project head, containing the accepted workspace result and
  complete successor Evolution Revision and Runtime Context Snapshot, commits
  only after all three outputs are ready;
- the admitted follow-up receipt pins and consumes the complete committed
  context;
- forced method, artifact-validation, and materialization failures each leave
  the prior project head active and expose no partial successor;
- pending and failed transitions reject next-task submission and never run it on
  stale context.

Evidence: successful and fault-injected transition reports.

### G7. Protected Performance

Environment: the canonical Terminal Bench manifests and pinned benchmark
configuration from Section 12.

Pass criteria:

- textual memory rescues at least 10 of 21 tasks;
- trajectory-to-skill rescues at least 12 of 25 tasks;
- agent-system evolution rescues at least 15 of 25 tasks;
- each method runs independently through the real Core path;
- protected behavior fixtures and source/resource guards show no unexplained
  algorithm change, and exact registry identities match the candidate;
- every applicable task has exactly one authoritative no-artifact baseline
  pass@1 result and one authoritative post-activation evolved pass@1 result;
- the baseline capture is sealed as that task's method input, and the evolved
  evaluation uses a fresh runtime with the exact canonical workspace restored;
- only same-task `0 -> 1` pairs contribute to the rescue floor; baseline
  successes, evolved failures, and infrastructure replacements remain visible;
- algorithm candidate trials are labeled separately and do not contribute to
  either authoritative arm or the rescue count.

Evidence: per-task records, aggregate reports, manifests, selected artifacts,
injection evidence, and exact release identities.

### G8. Recovery And Idempotency

The repository owns a versioned closed fault profile whose digest is bound by
the release manifest. A candidate cannot reduce it. The profile selects required
checkpoints across bootstrap/tunnel recovery; Task submit, admission, execution,
cancel, completion, retry, and close; evolution dispatch, artifact validation,
materialization, replacement, abandon, and commit; settings-only,
context-rebind, and historical-restore successors; and install/upgrade
activation. At those checkpoints it applies:

- Desktop termination;
- SSH tunnel loss;
- network interruption;
- Daemon termination;
- worker termination;
- task-runtime termination;
- server restart;
- repeated client mutation;
- concurrent Desktop ensure/upgrade operations;
- concurrent stale project mutations.

Each case binds the operation, checkpoint, fault, fixture identity, expected
durable state, operation-specific recovery deadline, and exact allowed
cardinalities. Every profile case is executed at least once on the candidate; an
unbounded “eventually recovers” assertion is insufficient.

Pass criteria:

- Desktop reconnects and replays events without duplicate runs;
- interrupted work reaches the declared honest terminal or recoverable state
  within its deadline;
- committed Project Heads, Evolution Revisions, and Runtime Context Snapshots
  remain valid;
- staged state never becomes partially active;
- orphan processes, runtimes, leases, and staging data reconcile safely;
- concurrent install or upgrade attempts leave one active generation and one
  Daemon writer;
- cancellation/completion races produce exactly one winner, a late attempt
  cannot become authoritative, and every retry receipt matches the task's
  closed admission;
- creating a replacement plan makes prior attempts ineligible before the new
  plan is visible, and only one predecessor compare-and-set can advance the
  head;
- restore rejects a stale expected head, inherits the unselected components,
  creates the required new identities, and invalidates drafts based on the old
  head;
- failed or interrupted settings-only, context-rebind, and restore successors
  leave the prior Project Head and draft unchanged; successful successors bind
  the exact required identities;
- repeated not-ready submission creates no Task or attempt, and the first valid
  submission after the exact successor is active creates exactly one Task and
  attempt.

Evidence: fault-injection and race test reports, immutable attempt receipts,
transition ledgers, and recovered state snapshots.

### G9. Upgrade And Rollback

The repository owns a versioned closed upgrade profile, bound by digest in the
release manifest. It pins every predecessor/newer fixture generation, metadata
fixture, active/quiescent state, compatibility outcome, injected failure, and
expected deadline/cardinality. It includes compatible-old, incompatible-old,
newer-Daemon, interrupted-stage, candidate-startup-failure,
migration-failure, pre-barrier rollback, post-barrier rollback rejection, and
concurrent-installer rows.

Pass criteria:

- compatibility decisions are derived from the negotiated manifest predicate,
  not version-string ordering alone;
- when a prior published compatible release exists, its exact Daemon fixture
  upgrades from the packaged Desktop;
- active compatible work is preserved or the upgrade is visibly deferred;
- the cross-version maintenance protocol safely observes quiescence and upgrades
  an incompatible-old fixture without business-state access;
- interrupted staging resumes safely;
- candidate startup failure retains the working generation;
- migration failure restores working metadata;
- pre-barrier rollback restores the old generation without data loss, while
  post-barrier rollback is rejected unless the profile proves backward
  readability and a lossless reverse migration;
- concurrent or stale installers cannot activate after the winning generation;
- Desktop never silently downgrades a newer Daemon;
- a registry/runtime-contract change creates a valid context-rebind successor
  for a compatible project or leaves that project visibly blocked on the prior
  Head with a typed repair action.

For the first External Beta, the published-predecessor upgrade row MAY be
recorded as `not_applicable` only when the publisher records an externally
verifiable source-repository release-history query showing no predecessor at
candidate freeze. Candidate failure, metadata rollback, concurrency,
incompatible-old, newer-Daemon, and no-silent-downgrade rows remain blocking and
use fixed fixture generations.

Evidence: upgrade matrix, before/after release identities, and rollback reports.

### G10. Desktop Product Quality

The repository owns a closed product-quality profile bound by digest in the
release manifest. It fixes workflow/state/window case IDs, exact viewport
dimensions, seeded remote fixtures, accessibility scanner/version/rules,
visual-snapshot method and tolerances, and manual keyboard/notification
assertions. Every case produces one indexed result.

Pass criteria:

- first-run, project creation, task execution, cancellation, retry, file
  transfer, preview, download/research export, project backup/restore,
  transcript, evolution, artifact diff, historical restore, System,
  diagnostics, Remove Server From This Mac, Delete Project From Server, Clean
  Shared Caches, and Uninstall OpenEvo Daemon workflows operate in the packaged
  app;
- empty, loading, offline, reconnecting, degraded, failed, cancelled, pending,
  and success states pass their profile assertions;
- every profile window has
  no clipped, overlapping, obscured, or unreachable controls;
- declared WCAG 2.2 AA contrast and focus checks pass, automated accessibility
  scanning reports no serious or critical finding, and a manual keyboard-only
  path completes setup, task submission, monitoring, and result export;
- notifications identify completion or required action without disclosing task
  or secret content on the lock screen by default;
- invalid capability and method states remain visible and block unsafe runs.

Evidence: packaged end-to-end results, visual snapshots, accessibility report,
and manual real-Mac checklist.

### G11. Security, Privacy, And Data Lifecycle

The repository owns a versioned closed security/data-lifecycle profile bound by
digest in the release manifest and checked against the protected release-policy
baseline. It fixes attack fixtures, secret-canary source/sink allow/deny rules,
network expectations, backup-corruption cases, and before/after data
inventories. Every case produces one indexed result.

Pass criteria:

- the profile covers host-key change, unauthenticated local
  access, remote port exposure, path traversal, symlink escape, reserved
  environment override, privileged runtime, and task attempts to read each
  forbidden credential or host resource; every case fails closed;
- a closed source-by-sink canary matrix assigns distinct values to Keychain,
  remote secret-store, configuration, environment, harness-authentication,
  harness-input, service-response, database-write, artifact, diagnostics, and
  export boundaries; every disallowed sink has zero matches in returned project
  config, non-secret databases, logs, events, timelines, errors, transcripts,
  datasets, artifact records or payloads, lineage, diagnostics, benchmark
  evidence, backups/exports, or release files;
- a designated Keychain or remote secret store contains only the secrets it is
  authorized to retain, is excluded from export, and passes permission and
  access-boundary tests;
- task-authored code and tool subprocess attack fixtures cannot read SSH keys,
  Docker access, the complete user home, Daemon state, raw subscription
  credentials, or Hugging Face credentials;
- no analytics, crash, telemetry, or diagnostics upload occurs by default;
  disclosed Subscription harness traffic, user-enabled task networking, and
  release-managed downloads are tested separately and are not telemetry;
- project backup contains no secret, rejects corruption without partial restore,
  and restores the declared authoritative inventory;
- Delete Project From Server, Clean Shared Caches, Remove Server From This Mac,
  and each Uninstall OpenEvo Daemon choice produce before/after inventories
  proving that only the stated owned data changed;
- diagnostics exclude research content by default.

Evidence: security test report, canary scan, network binding inspection, and
data-lifecycle test report.

### G12. Release Consistency

Pass criteria:

- DMG, Daemon Bundle, Codex CLI, managed images, model profile, manifest,
  checksums, source tag, runtime descriptors, release notes, and tested
  identities agree;
- all blocking reports refer to the same candidate;
- user and maintainer documentation match actual behavior and supported scope;
- known limitations include unsigned status and every intentionally unsupported
  capability;
- dependency evidence satisfies the vulnerability freshness, severity,
  exception, and license policy in Section 14;
- the non-public draft or staging release contains the exact validated
  artifacts, and the publisher uses a no-replace publication operation that
  changes visibility without accepting new tag or asset bytes;
- downloaded-draft revalidation completes without accepting an undeclared,
  missing, replaced, or mismatched asset or metadata field.

Evidence: release-manifest verifier output, dependency/security reports,
downloaded draft-release smoke, and detached G12 attestation. The attestation
is not a member of the draft asset set it attests. The final candidate evidence
index is generated only after this gate ends and is not G12 evidence.

## 16. Explicitly Out Of Scope

The External Beta does not include:

- changing or improving evolution algorithms;
- parameter, LoRA, adapter, or other parametric evolution;
- in-session or streaming evolution;
- harnesses other than Codex;
- arbitrary external API-key-and-base-URL task execution;
- arbitrary unvalidated Hugging Face model profiles;
- user-installed unverified plugins from Desktop;
- a public CLI or Dev Kit product;
- PyPI distribution as a product surface;
- benchmark controls in Desktop;
- multi-user or organization-level Daemon administration;
- Intel Mac, Windows Desktop, or Linux Desktop;
- arbitrary Linux distribution support;
- guaranteed automatic installation of drivers, kernels, Docker system policy,
  firewalls, or other root-owned infrastructure;
- bundling large model weights inside the DMG;
- continuous remote backup or disaster-recovery service;
- macOS signing, notarization, or automatic Desktop updates;
- compatibility with pre-release legacy runtime state, names, markers, or
  import paths.

An out-of-scope capability MAY remain in source for research or future work,
but it MUST NOT be enabled, documented as supported, or used as release
evidence.

## 17. Change Control

This file is the single source of truth for the product specification.

Change this specification only when changing:

- the product boundary or terminology;
- the supported environment or execution mode;
- an ordinary-user workflow;
- the Desktop-Daemon ownership or contract;
- an evolution target, protected behavior, or activation invariant;
- security, privacy, or data-lifecycle behavior;
- release artifacts or blocking acceptance.

Do not add implementation progress, branch names, phase labels, class names,
database protocols, issue-by-issue changelogs, or detailed endpoint schemas.
Those belong in the non-normative implementation plan, issues, tests, API
contracts, architecture documents, and module READMEs.

If implementation exposes an unresolved product decision, amend this file once
and make all implementation documents conform to it. Conflicting documentation
is a defect; it does not create an alternative product contract.

Algorithm changes and changes to the established Core execution/evolution
architecture require a separate research or architecture decision and cannot be
smuggled into productization, repository cleanup, Desktop work, or release
engineering.
