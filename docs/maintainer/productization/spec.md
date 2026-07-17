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
OpenEvo Daemon
|- versioned control API
|- project, task, run, and revision owner
|- gateway, rollout, evolution, and worker services
|- Codex harness runtimes
`- managed vLLM deployment when self-deployed
       |
       v
OpenEvo Core implementation
```

`src/openevo/` is the shared Core implementation used by the Daemon. Core is
not a third application that ordinary users install or operate separately.

The Desktop sidecar is a client-internal transport and state-projection
component. It is not a business backend. It MUST NOT own canonical project,
run, artifact, capability, or revision state.

Benchmark automation is maintainer tooling outside both products. It consumes
Core or Daemon contracts and never becomes a Desktop feature.

The repository dependency direction is:

```text
Desktop Client -> versioned Daemon contracts
OpenEvo Daemon -> Core implementation
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
- **Project Head**: the workspace snapshot, evolution revision, and verified
  effective execution snapshot that the next workspace draft is based on. A
  submitted task uses a sealed snapshot derived from that base and the exact
  effective execution snapshot in the head. The head advances as one auditable
  transition and never rewinds.

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

Both execution modes require a release-supported Docker Engine API available to
the remote user. Self-Deployed additionally requires a compatible NVIDIA
driver, NVIDIA Container Toolkit, sufficient VRAM, and sufficient model
storage. Each release carries a versioned host profile with the exact Docker,
driver/CUDA, VRAM, memory, disk, and model constraints enforced by preflight.

The release MUST preserve the established Core responsibility and data flow
across gateway, rollout, runtime, capture, trajectory, evolution backend,
worker, artifact, context resolution, and runtime injection. Productization
may reorganize paths, packaging, imports, adapters, and public interfaces, but
MUST NOT redesign or replace the validated evolution algorithms.

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
-> verify the SSH host identity
-> run preflight and install or attach the Daemon
-> configure project execution settings
-> prepare Codex or a self-deployed model
-> create a research project
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
server address and SSH identity
-> host-key confirmation
-> remote preflight
-> Daemon installation or attachment
-> execution-mode setup
-> service readiness
-> first project
```

Supported SSH authentication methods are:

- SSH agent;
- private key with an optional Keychain-backed passphrase;
- password with a Keychain-backed credential.

A changed host key MUST block connection until the user explicitly reviews and
accepts the new identity. Desktop MUST NOT silently replace a pinned host key.

Remote network settings include:

- HTTP proxy;
- HTTPS proxy;
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
- run, cancel, infrastructure-retry, and run-again actions with their distinct
  task semantics;
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
Ready for next task
Needs attention
Cancelled
```

Technical service names and stack traces are hidden by default but available in
an explicit technical-details view.

The user MAY prepare a draft for the next task while a run or evolution is in
progress. During that interval the draft is limited to local task text and
non-file form values; remote workspace uploads and mutations wait for the new
head. Desktop MUST NOT start the task until the required project head is ready
or the user has explicitly resolved a failed evolution transition.

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
- browsing project inputs, task outputs, and evolution artifacts;
- safe preview of supported text, Markdown, JSON, image, and tabular formats;
- single-file download and directory export.

Internal OpenEvo state is not editable through the file browser.

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
- project deletion, cache cleanup, and Daemon uninstall.

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
- a Docker Engine version and API from the release host profile, usable by the
  remote user;
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

Installation is an idempotent, journaled state machine:

```text
preflight
-> stage exact bundle
-> verify release identity and digest
-> install immutable generation
-> verify Core and registry inventory
-> start candidate Daemon
-> run readiness checks
-> atomically activate generation
-> retain previous working generation
```

The transfer path MUST work over standard SSH/SFTP and MUST NOT require
`rsync`, Python, or a package manager on the server.

An interrupted or repeated installation resumes safely or restarts from a
clean staging area. An unverified generation never becomes active. A failed
candidate never replaces a working Daemon.

### 8.5 Process And Service Model

The Daemon is the single writer for its managed state and the supervisor for
its child processes:

```text
OpenEvo Daemon
|- control API and operation manager
|- project/run/revision owner
|- gateway
|- rollout
|- evolution backend
|- evolution workers
|- optional vLLM deployment
`- managed task runtimes containing Codex
```

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
- An incompatible upgrade MUST wait for active work or require an explicit
  cancellation decision.
- Desktop MUST NOT silently downgrade a newer Daemon.
- An incompatible pairing is read-only where safely possible and otherwise
  blocked with a typed update action.

Upgrade creates an immutable candidate generation, backs up mutable metadata
needed for recovery, performs required migrations, and proves readiness before
activation. Candidate or migration failure restores the previous working
generation and leaves project data, artifacts, model caches, and Codex
authentication intact.

### 8.7 Automated Preparation

Within the supported host baseline, OpenEvo prepares:

- the Daemon and user-space dependencies;
- a pinned supported Codex CLI;
- managed science runtime images;
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
- Docker Engine access;
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
manifest-bound Hugging Face model profile. The Daemon:

1. resolves a mutable branch or tag to an exact commit;
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
context limit, and compatibility range. Desktop MAY offer an explicitly
experimental custom Hugging Face model ID and revision, but that path is not a
release-supported profile, cannot contribute release evidence, and never
inherits compatibility claims from a static preflight result.

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

Before a healthy Daemon exists, Desktop uses SSH only for:

- host inspection;
- bundle staging and installation;
- unique Daemon ensure or attachment;
- private tunnel establishment;
- recovery when the control API cannot start.

After the Daemon is healthy, all project, task, file, service, model, evolution,
artifact, and data-management operations use the versioned Daemon API. Desktop
MUST NOT fall back to ad hoc SSH business commands when an API request fails.

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
- **Project Heads And Revisions**: immutable workspace/evolution composition
  and historical execution context;
- **Artifacts**: safe metadata, content, renderer payloads, diffs, and lineage;
- **Services**: Codex, Gateway, worker, runtime, and model health;
- **Diagnostics and Data Management**: doctor, repair, export, cleanup, delete,
  and uninstall operations.

Exact routes and payloads belong to the versioned API contract and code. These
resource semantics are product requirements.

### 10.3 API Behavior

The contract MUST provide:

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

The Daemon is the authority. Desktop MUST NOT infer success from SSH process
output, use a local method table, rebuild capabilities from bundled code, or
substitute stale cache when the remote contract fails.

### 10.4 Project Validation And Run Admission

Desktop saves desired project execution and evolution configuration before
submitting a task. The Daemon validates it against server readiness and the
same verified capability registry that will execute it.

When desired execution settings differ from the active head, the Daemon first
resolves and proves a new effective snapshot, validates active artifacts
against it, and commits a settings-only successor head that inherits the
workspace and evolution revision. This transition is allowed only when no task
or other head transition is in flight. A task cannot override mode, model
deployment, or network policy; it uses the exact effective snapshot in its
predecessor head.

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
verified effective execution snapshot for harness, capture, model, runtime,
serving, and task-network policy
verified registry and normalized initial evolution intent
```

Unsaved Desktop draft state, later project edits, service changes, new
artifacts, or model changes MUST NOT alter it. Every infrastructure run attempt
for that task references the same admission record.

Every attempt produces an immutable execution receipt binding the admission
identity to the actual predecessor head, workspace, harness, capture, model,
runtime, serving, network-policy, and service-attestation identities it used.
An attempt cannot become authoritative unless the receipt exactly satisfies
the closed admission.

### 10.5 Task, Attempt, Workspace, And Head State

A project has one linear active head. It MAY have one submitted task or one
successor transition in flight at a time; different projects MAY run
concurrently. The release does not expose project-head branches.

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

The first attempt that reaches a terminal harness result with valid required
capture becomes authoritative for the task, even when the scientific outcome
is a failure. It closes the task to further attempts and is the only attempt
allowed to produce that task's workspace result and evolution transition.
Running the request again after that point creates a new task based on the
project head that eventually commits.

The harness executes in an isolated writable view of the sealed input snapshot.
Its file changes produce an immutable result snapshot. Desktop MAY display and
download those results immediately, but they do not become inputs to another
task until the successor project head commits.

User uploads and file edits between tasks create a Daemon-owned staged
workspace draft with an expected base-head identity. Submission seals that
draft. A submitted task or successor transition blocks further remote workspace
mutation for the project. If the expected head no longer matches, submission
fails visibly and requires the user to restage against the current head; no
automatic merge or silent data loss is permitted.

One run-result project-head transition combines the accepted workspace-result
snapshot with the successor evolution revision and inherits the exact effective
execution snapshot from the predecessor head. When no evolution target is
enabled, the transition commits the workspace result with inherited evolution
artifacts. When evolution is enabled, the next task waits for the complete
transition. A separate settings-only transition is the only path that changes
the head's effective execution snapshot.

Historical restore is allowed only when no task or successor transition is in
flight. It uses an expected-current-head check and creates one atomic audited
successor. Selected historical workspace or evolution content is copied into
that successor; each unselected component is inherited from the current head.
The effective execution snapshot is always inherited. Changing the artifact set
creates a new evolution-revision identity. Restore never rewinds or forks the
project head and invalidates any local draft based on the prior head.

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
differ. A Daemon generation required by an unresolved transition cannot be
discarded by upgrade.

Repair configuration is transition-specific. Desktop MUST ask separately
whether to save the same selection as the project's desired configuration for
later tasks; saving it does not mutate the active or replacement plan.

The release targets are:

| Target ID | User-facing name | Runtime effect |
| --- | --- | --- |
| `text_memory` | Textual Memory | Adds natural-language long-term memory to the next harness context |
| `skill_bundle` | Skills | Adds a validated skill bundle containing `SKILL.md` |
| `agent_system` | Agent System | Adds validated harness instruction content at an allowlisted destination |

Project configuration uses one generic target map:

```yaml
evolution:
  targets:
    text_memory:
      enabled: true
      method: text_memory_expel_reflector
      config: {}
    skill_bundle:
      enabled: true
      method: skill_bundle_reflector
      config: {}
    agent_system:
      enabled: true
      method: agent_system_gepa_reflector
      config: {}
```

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

Every compiler-owned configuration field MUST be declared by the method
descriptor as a closed-source injection from an implemented authoritative
project or execution source. The field remains present in the method's complete
schema but Desktop does not render it as user-owned input. The Daemon removes
stale user values, injects the authoritative value, and validates the complete
normalized method configuration before planning. An undeclared field with the
same name remains user-owned and cannot be overwritten by convention.

Every exposed method descriptor declares:

```text
stable method ID
target ID
user-facing metadata
closed configuration schema and defaults
ordered input requirements
output artifact types
supported harness, execution, capture, and runtime profiles
compiler-owned configuration injections
implementation and distribution identity
maturity and exposure
```

The release registry is built from exact verified release distributions.
Desktop does not automatically install arbitrary research plugins, and the
product does not claim that method code is sandboxed from the server user.

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

After a verified Daemon upgrade, a compatible new method appears through
capabilities and Desktop renders its configuration without a Desktop code
change.

Adding a new target is broader. It requires a typed artifact contract, safe
projection and runtime-consumption behavior, presentation metadata, and a
supported safe renderer. Target-specific behavior lives only in verified
descriptors and data-only handlers. When a target fits the generic
contribution and renderer contracts, adding it MUST NOT add target-ID branches
to the project compiler, resolver, materializer, Gateway, or Desktop.

Existing method functions MAY be reached through framework adapters. Their
algorithm implementation MUST NOT be rewritten merely to satisfy packaging or
dispatch structure.

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

The release owns a canonical protected-baseline manifest tied to the
pre-productization commit. It identifies every behavior-bearing function body,
prompt/resource, default/config value, filter, evaluator/selection rule,
tie-break rule, and artifact fixture by normalized digest. Candidate
comparison is fail closed.

The only permitted differences are a closed machine-readable allowlist of:

- file or module relocation with normalized protected content unchanged;
- import-path rewrites required by that relocation;
- framework adapter and descriptor code outside protected algorithm bodies.

Any other protected difference blocks release even when tests or benchmark
floors pass. Human intent is not the acceptance predicate; review confirms the
machine-generated comparison and allowlist rather than deciding whether an
algorithm change was intentional.

### 11.5 Cross-Session Transition

Every evolution method, including parametric methods when supported, takes
effect only across sessions:

```text
run N is admitted on project head H containing workspace W, evolution
revision R, and effective execution snapshot E
-> Codex executes entirely on W, R, and E
-> transcript or trajectory is captured
-> the transition dataset is sealed
-> enabled methods run outside active inference
-> all outputs validate and materialize
-> successor project head H+1 containing W+1, R+1, and inherited E commits
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
- the prior project head and evolution revision remain committed and
  recoverable.

If the next task requires a pending successor, it is visibly queued or not
ready. It MUST NOT silently run on the prior project head. After a failed
transition:

- retrying transient execution preserves the same immutable plan and records a
  new transition attempt;
- changing method configuration or disabling a target creates a new immutable
  plan bound to the same predecessor, authoritative run, sealed dataset, and
  workspace-result snapshot;
- explicitly abandoning evolution creates a successor project head containing
  the accepted workspace result and inherited evolution artifacts.

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

It consumes stable Core or Daemon capabilities for project compilation,
execution, capture, evolution, artifact activation, and follow-up runs.

Terminal Bench, scientific benchmarks, and general-agent benchmarks use sibling
automation packages. Desktop exposes no benchmark controls.

Release performance evidence MUST exercise the real data path through capture,
dataset sealing, verified method dispatch, artifact registration,
materialization, revision activation, runtime injection, and the authoritative
follow-up harness attempt. Directly calling a method function is unit-test
evidence, not release performance evidence.

### 12.1 Performance Manifests

The repository MUST contain canonical, reviewable manifests for the three
historical baseline-failed subsets. Each manifest pins or identifies:

- benchmark and task versions;
- applicable task IDs;
- runtime image;
- harness and model configuration;
- method configuration;
- evaluator version;
- canonical protected baseline and behavior-fixture bundle identities;
- genesis project head, predecessor evolution revision, and complete prior
  artifact set;
- source-attempt, capture, sealed-dataset, and dataset-construction identities;
- algorithm candidate/evaluator budget;
- allowed infrastructure-replacement policy and complete launch-ledger format;
- authoritative result format.

These historical preservation profiles use the execution mode and model
identity recorded by the canonical baseline, currently Codex Subscription.
Their floors protect the previously demonstrated method behavior; they are not
silently transferred to a different Self-Deployed model.

Each method runs independently with no artifact from another target. The score
is the number of tasks rescued by one authoritative evolved pass@1 attempt.
That attempt runs only after the protected algorithm has completed its internal
candidate evaluation, selected its result, and the result has been activated in
the successor revision. Algorithm-internal candidate trials are not gate
attempts and cannot be counted as rescues.

The gate profile sets `n_attempts=1` for the authoritative follow-up. Evidence
labels algorithm evaluation records separately from the
`authoritative_gate_score`.

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

Gate profiles use a closed machine-readable schema and record both
`historical_reference` and `blocking_floor`, together with exact task, runtime,
harness, model, method, evaluator, predecessor, dataset, budget, attempt-count,
and result-format identities. Unless a profile explicitly pins a prior artifact,
the complete initial artifact set is empty. The aggregator fails closed on an
extra scored attempt, an unclassified replacement, or a record not reachable
from the declared launch ledger. Historical-summary-only manifests are not
executable release evidence.

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
- Runtime mounts are limited to the project workspace and the materialized
  execution snapshot.
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

Redaction MUST be tested with distinct canary values at configuration,
environment, harness, service-response, database, artifact, and export
boundaries.

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

Removing a server profile from Desktop removes local connection data only. It
does not delete remote research data.

Deleting a project is one idempotent Daemon-owned asynchronous operation. It
first prevents new project work, then stops or waits for conflicting work,
removes the project's managed workspace, history, datasets, artifacts, and
revision metadata across every owning store, and verifies the final state
before reporting success. A partial delete remains visible and safely
retryable. Shared verified model and image caches are not deleted with one
project.

Daemon uninstall offers distinct, explicitly confirmed choices:

- remove the Daemon and retain projects and caches;
- remove the Daemon and projects but retain shared model caches;
- remove all OpenEvo-managed data.

Cache cleanup, project deletion, and uninstall are separate operations.
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
- complete dependency, license, and vulnerability evidence for shipped
  components.

The release manifest binds:

```text
Desktop version and architecture
Daemon and protocol version
Core and verified registry identity
managed science runtime image digest
vLLM image digest
validated self-deployed model profile
validated remote-host profile
supported platform matrix
artifact checksums
```

The DMG contains the matched Daemon Bundle or obtains only the exact
manifest-bound bytes through a verified download. Remote bootstrap verifies the
bundle before installation.

PyPI is not an External Beta release surface. Editable source installation is a
maintainer workflow and MUST NOT be documented as Desktop or Daemon
installation.

The DMG is unsigned and non-notarized for this release. User documentation
provides concise manual macOS launch instructions without implying that the
application is signed.

Release security evidence uses vulnerability data refreshed within seven days
of candidate evaluation. No unresolved known Critical or High vulnerability
may affect a shipped reachable runtime component. No shipped dependency may
have an unknown, incompatible, or prohibited license. A Medium-or-lower
vulnerability exception requires a linked issue, affected-component analysis,
owner, expiry, and release-note disclosure; an exception is part of the
candidate evidence index.

Final artifacts are first uploaded to a non-public draft or staging release.
The publisher downloads and revalidates those exact bytes and all blocking
evidence before making the release public. Publication cannot replace the tag,
manifest, or asset bytes; any change creates a new candidate and reruns the
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
- diagnostics, retention, deletion, cache cleanup, and uninstall;
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

One machine-readable candidate evidence index binds the release manifest
digest, candidate commit, artifact hashes, environment identities, report
hashes, CI or operator-run references, `simulator=false` for every release
gate, and the final status of G1 through G12. The release publisher downloads
and revalidates the indexed evidence and refuses publication when any gate is
missing, pending, duplicated with conflicting identity, simulated, or bound to
another candidate.

### G1. Product And Contract Boundary

Pass criteria:

- only Desktop and Daemon are presented as applications;
- Core has no dependency on Desktop or benchmark packages;
- benchmark-specific code is outside Core and Desktop;
- no public CLI, Dev Kit, PyPI install path, or legacy product identity is
  advertised;
- packaged Desktop uses only versioned remote capabilities and APIs;
- no local method table or release fallback backend is active;
- capability conformance covers visible methods, hidden accepted methods,
  selection resolvers and every declared concrete outcome, configured and
  effective defaults including no-effective-default, and independent
  execution/capture/harness/runtime support reasons;
- hidden accepted selections round-trip without becoming new recommendations,
  and resolver identities/support match their accepted concrete methods;
- compiler-owned injection removes stale user values and supplies the
  authoritative harness/provider identity in both execution modes;
- fixture extensions prove that a compatible method or generic target appears
  without a Desktop method table or target-ID branch in compiler, resolver,
  materializer, Gateway, or Desktop.

Evidence: repository checks, package/import tests, release-content inspection,
capability conformance suites, generic-extension fixtures, and documentation
link checks.

### G2. Packaged Desktop Installation

Environment: a clean user account on every macOS version claimed by the release
manifest, with no repository checkout, Python, Node, Rust, or OpenEvo state.

Pass criteria:

- the DMG mounts, installs, and launches through the documented unsigned-app
  flow;
- the bundled native host and Desktop sidecar start without a terminal;
- first-run, quit, relaunch, and uninstall work;
- no development server, simulator, or source-relative fallback is used.

Evidence: packaged-app smoke logs, screenshots, artifact checksum, and tested
machine identity.

### G3. Clean Subscription Deployment

Environment: each claimed subscription-capable Linux host profile, with SSH
and release-supported Docker Engine but no usable NVIDIA GPU, OpenEvo, Python,
`rsync`, Codex, or managed runtime image.

Pass criteria:

- Desktop installs and attaches the exact Daemon Bundle;
- Codex and the runtime image are prepared through Desktop;
- the user completes the mediated subscription login;
- a real task runs with transcript capture;
- Desktop disconnect and reconnect preserve the run and timeline.

Evidence: preflight report, installation operation history, service health,
task/run record, transcript-capture declaration, and packaged Desktop trace.

### G4. Clean Self-Deployed Deployment

Environment: every manifest-claimed self-deployed host/model profile, with the
required driver, Docker Engine, and NVIDIA runtime but no OpenEvo, Python,
`rsync`, Codex, managed science runtime image, vLLM image, or model cache.

Pass criteria:

- Desktop installs the Daemon;
- Codex, the managed science runtime image, the exact reference model, and the
  vLLM image are prepared and verified through Desktop;
- serving readiness proves the expected model identity;
- Codex executes a real task through Core Gateway and vLLM;
- interruption and resume of at least one large download are demonstrated.

Evidence: hardware/preflight report, model snapshot identity, image digest,
service readiness, run snapshot, and packaged Desktop trace.

### G5. Mode-By-Target Evolution

Matrix: both execution modes multiplied by each of the three release targets,
with one target enabled per test.

Pass criteria for each of six paths:

- a predeclared target-specific canary fails its criterion without the target
  artifact under the same mode/profile;
- run N produces valid capture and a sealed transition input;
- the selected protected method runs through verified dispatch;
- its typed artifact validates and materializes;
- successor project head H+1 and evolution revision R+1 commit;
- run N+1 is admitted on H+1;
- runtime evidence proves that Codex consumed the artifact and the follow-up
  canary now passes the target-specific criterion;
- method execution evidence proves the protected reflector used the
  compiler-injected harness/provider identity for that execution mode rather
  than a user-supplied model endpoint.

Evidence: six machine-readable transition reports plus corresponding task,
baseline-canary, artifact, project-head, revision, and follow-up efficacy
records.

### G6. Atomic Combined Evolution

Pass criteria:

- all three targets can evolve from one completed run;
- independent jobs may overlap without state corruption;
- one successor project head, containing the accepted workspace result and
  complete successor evolution revision, commits only after all three outputs
  are ready;
- injected follow-up context contains the complete committed set;
- forced method, artifact-validation, and materialization failures each leave
  the prior project head active and expose no partial successor;
- pending and failed transitions never silently admit a run on stale context.

Evidence: successful and fault-injected transition reports.

### G7. Protected Performance

Environment: the canonical Terminal Bench manifests and pinned benchmark
configuration from Section 12.

Pass criteria:

- textual memory rescues at least 10 of 21 tasks;
- trajectory-to-skill rescues at least 12 of 25 tasks;
- agent-system evolution rescues at least 15 of 25 tasks;
- each method runs independently through the real Core path;
- the canonical protected baseline, behavior-fixture bundle digest, protected
  behavior suites, exact registry identities, and normalized candidate
  comparison contain no difference outside the closed mechanical allowlist;
- every applicable task has one authoritative scored pass@1 result.
- algorithm candidate trials are labeled separately and only the independent
  post-activation attempt contributes to the rescue count.

Evidence: per-task records, aggregate reports, manifests, selected artifacts,
injection evidence, and exact release identities.

### G8. Recovery And Idempotency

Fault matrix:

- Desktop termination;
- SSH tunnel loss;
- network interruption;
- Daemon termination;
- worker termination;
- task-runtime termination;
- server restart;
- repeated client mutation.

Pass criteria:

- Desktop reconnects and replays events without duplicate runs;
- interrupted work reaches an honest terminal or recoverable state;
- committed revisions remain valid;
- staged state never becomes partially active;
- orphan processes, runtimes, leases, and staging data reconcile safely;
- cancellation/completion races produce exactly one winner, a late attempt
  cannot become authoritative, and every retry receipt matches the task's
  closed admission;
- creating a replacement plan makes prior attempts ineligible before the new
  plan is visible, and only one predecessor compare-and-set can advance the
  head;
- restore rejects a stale expected head, inherits the unselected components,
  creates the required new identities, and invalidates drafts based on the old
  head;
- a settings-only successor proves readiness and compatibility before changing
  the head's effective execution snapshot.

Evidence: fault-injection and race test reports, immutable attempt receipts,
transition ledgers, and recovered state snapshots.

### G9. Upgrade And Rollback

Pass criteria:

- a supported older Daemon upgrades from the packaged Desktop;
- active compatible work is preserved or the upgrade is visibly deferred;
- interrupted staging resumes safely;
- candidate startup failure retains the working generation;
- migration failure restores working metadata;
- Desktop never silently downgrades a newer Daemon.

Evidence: upgrade matrix, before/after release identities, and rollback reports.

### G10. Desktop Product Quality

Pass criteria:

- first-run, project creation, task execution, cancellation, retry, file
  transfer, transcript, evolution, artifact diff, history, System, and
  diagnostics workflows operate in the packaged app;
- empty, loading, offline, reconnecting, degraded, failed, cancelled, pending,
  and success states are coherent;
- supported window sizes have no clipped, overlapping, or unreachable controls;
- keyboard navigation, focus, contrast, and notifications pass the declared
  accessibility checks;
- invalid capability and method states remain visible and block unsafe runs.

Evidence: packaged end-to-end results, visual snapshots, accessibility report,
and manual real-Mac checklist.

### G11. Security, Privacy, And Data Lifecycle

Pass criteria:

- host-key change, unauthenticated local access, remote port exposure, path
  traversal, symlink escape, reserved environment override, and privileged
  runtime attempts fail closed;
- distinct secret canaries are injected at configuration, environment,
  harness-authentication, harness-input, and service-response boundaries and
  do not persist in returned project config, databases, logs, events,
  timelines, errors, transcripts, datasets, complete artifact records or
  payloads, lineage, diagnostics, benchmark evidence, or release files;
- task-authored code and tool subprocesses cannot read or exfiltrate SSH keys,
  Docker access, the complete user home, Daemon state, raw subscription
  credentials, or Hugging Face credentials;
- no telemetry or upload occurs by default;
- project deletion, cache cleanup, profile removal, and each uninstall choice
  affect only the stated data;
- diagnostics exclude research content by default.

Evidence: security test report, canary scan, network binding inspection, and
data-lifecycle test report.

### G12. Release Consistency

Pass criteria:

- DMG, Daemon Bundle, manifest, checksums, source tag, runtime descriptors,
  release notes, and tested identities agree;
- all blocking reports refer to the same candidate;
- user and maintainer documentation match actual behavior and supported scope;
- known limitations include unsigned status and every intentionally unsupported
  capability;
- dependency evidence satisfies the vulnerability freshness, severity,
  exception, and license policy in Section 14;
- the non-public draft or staging release contains the exact validated
  artifacts, and the publisher proves that public release will expose those
  immutable bytes without replacement;
- the release is marked publish-eligible only after this downloaded-draft
  validation passes.

Evidence: release-manifest verifier output, dependency/security reports, and a
downloaded draft-release smoke test. These are the pre-publication G12
eligibility records.

## 16. Explicitly Out Of Scope

The External Beta does not include:

- changing or improving evolution algorithms;
- parameter, LoRA, adapter, or other parametric evolution;
- in-session or streaming evolution;
- harnesses other than Codex;
- arbitrary external API-key-and-base-URL task execution;
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
