# Desktop Product Renderer Boundary

## v0.1.9 Desktop v2 release boundary

The release renderer is backed by `LocalApiDesktopProductProviderV2`. Remote
workspace profiles contain only a literal alias discovered from the user's
`~/.ssh/config`; the sidecar delegates routing, identity, authentication, and
trust to system OpenSSH. Once the project tunnel is active, project, Task,
timeline, capability, validation, service, Project Head, Evolution Revision,
and Runtime Context reads use Desktop/Core v2 contracts only.

Core v2 publishes Task-scoped artifact metadata and bounded artifact content
metadata through the active project tunnel. The renderer aggregates every
bounded Task artifact page into the authoritative snapshot, rejects cross-project
or inconsistent duplicate identities, and uses the verified Desktop v2 content
and diff routes for on-demand inspection. It never falls back to Desktop v1,
direct SSH, or a direct Core URL.

## Authoritative data only

The product renderer contains no built-in Project, Session, transcript, artifact,
workspace, capability, or evolution sample data. An empty or unavailable backend
renders an explicit empty/error state. Product runtime paths never fall back to a
fixture, simulator, legacy provider, or curated scientific story.

Deterministic fixtures remain confined to automated contract tests and are not
imported by either the live development entry or the packaged release renderer.

The product renderer consumes only `DesktopProductProvider`. Mutations carry the
renderer-observed stream epoch, resource ETag, and a stable action identity.
`startRun` intentionally carries only project identity and intent metadata; the
Local API owner must perform project snapshot, capability, validation, and
revision handshakes.
`retryRun` is a separate release mutation for an existing failed run. It carries
that run's identity and ETag and must use
`POST /desktop/v1/runs/{run_id}/retry`; retry never falls back to `startRun` or
creates a replacement run. Non-release fixtures may omit the mutation, but
release provider construction fails closed unless the adapter implements it.

Release startup has one entry point: `createReleaseDesktopProductProvider`.
It accepts a provider only after the Tauri bootstrap and `DesktopApiClientV1`
agree on contract major, checked-in OpenAPI digest, provider kind, and required
features. The contract simulator is test-only and is not a release fallback.
The final native renderer acknowledgement is deferred until the provider has
loaded an authoritative snapshot and React has committed the actual product
shell. A bootstrap placeholder or provider object alone cannot satisfy the
packaged app smoke; acknowledgement failure tears down that exact sidecar
session and returns to the explicit startup failure state.
Packaged bootstrap also reports a closed diagnostic sequence through a typed
native command. `bootstrap_context_*`, `local_api_version_*`,
`retry_recovery_*`, and `provider_adapter_*` identify the exact provider
construction boundary; `provider_created`, `provider_create_failed`,
`initial_snapshot_failed`, and `product_committed` cover the surrounding shell,
initial data load, and React publication. Every `*` pair is restricted to its
fixed `validated`/`verified`/`ready` or `failed` value. These stages are
diagnostic only: none can replace the final native renderer acknowledgement or
make a candidate pass. Reporting is best effort and cannot change product
readiness or error handling, and no error text, endpoint, credential, or user
data crosses this diagnostic channel.
The `openevo-desktop` Vite mode replaces the general provider-kind parser with
`providerKinds.release.ts`, whose only accepted value is `desktop_sidecar`.
Rollup can then remove simulator, scaffold, and dry-run provider definitions and
their strings from the packaged renderer. Normal typecheck and unit tests
continue to use `providerKinds.ts`; test-only contract samples do not become
release dependencies or a renderer fallback.

`/product-preview.html` is a live remote-agent development entry only. It
requires Vite mode `openevo-live-agent` and a reachable authenticated daemon;
opening it from plain Vite mode fails immediately instead of displaying a
sample Project, Session, artifact, capability, or agent response. The Tauri
release build continues to start from `index.html`.

Native development uses `npm run tauri:dev`. Tauri starts Vite through
`dev:openevo`, which selects the same product-only entrypoint as the release
build before attaching the native bridge. The plain `npm run dev` command
remains available for the separate shared observability pages; it is not the
Desktop native development surface.

For a real remote-development loop, use `npm run dev:live` from Linux/WSL or
macOS after installing `uv`. This command never imports the fixture provider.
It launches the same Tauri renderer and authenticated Local API v2 adapter used by the product,
then lets the sidecar deploy/connect the sealed Daemon over the selected system
OpenSSH alias. It requires two explicit local inputs:

- `OPENEVO_DEV_RELEASE_ASSETS_ROOT`: a staged `openevo-release-assets`
  directory containing the Core wheel/framework lock, Linux Daemon bundle and
  managed runtime archive;
- `OPENEVO_DEV_ASKPASS_HELPER`: an absolute path to a link-count-one `0755`
  `openevo-ssh-askpass` executable built for the local host.

The fixed Vite origins are admitted only when the sidecar reports the
`development` build channel. Release and test compositions retain the closed
Tauri-only origin set. There is no visual-fixture or sample-data launch command.

The first formal-Daemon migration entrypoint is `npm run dev:formal`. It builds
the current source-development Core wheel and Linux Daemon bundle, downloads and
verifies the pinned managed runtime once, builds the native askpass helper, and
then delegates to `dev:live`. Clean inputs are cached by full Git commit under
`~/.cache/openevo/formal-desktop`. A dirty checkout is allowed only on this
explicit development path and receives a content-derived `dirty` cache identity,
so edited or untracked source bytes cannot reuse clean assets. It remains marked
as build channel `development`; release packaging still requires an immutable,
clean commit. Assets from another commit remain rejected. This path uses the
authenticated Desktop `/desktop/v2` sidecar and tunneled Daemon `/v2` APIs. It
never starts
`scripts/dev/live_agent_daemon.py`. The existing `dev:agent:remote` command is
retained temporarily for development-only features that have not yet moved to
the immutable formal workspace and artifact contracts.

To run the same formal Sidecar in the system browser instead of a Tauri WebView,
use `npm run dev:formal:browser` on macOS or Linux. The command builds the real
renderer, starts an authenticated loopback Sidecar, opens `/openevo` in the
default browser, and keeps all SSH and Daemon lifecycle work in that local
process. Remote workspaces are selected only from literal aliases discovered in
the current user's system `~/.ssh/config`. Users configure `HostName`, `User`,
`Port`, `IdentityFile`, `ProxyJump`, and related routing or authentication
settings with OpenSSH, verify `ssh <alias>` independently, then rescan and select
that alias in OpenEvo. The React API never accepts raw server coordinates,
passwords, passphrases, private keys, SSH commands, remote tokens, or host paths.
Authentication remains owned by system OpenSSH, ssh-agent, macOS Keychain, and
the sealed native askpass helper. The Sidecar invokes the selected alias and owns
Daemon bootstrap and the authenticated tunnel. This source-development launcher
is the browser-hosted product path, not a browser-only SSH implementation; a
distributable build still needs to package and start the local Sidecar on the
user's machine.

For the narrower "ask the remote Codex and evolve selected documents" loop, use
`npm run dev:agent`. This mode bypasses the sealed release lifecycle, but obtains
its target, method, support, schema, and renderer metadata from the Core framework
catalog at `GET /openevo-dev-agent/v1/capabilities`. The response is explicitly
marked `development_catalog_unverified`; it is not a verified release registry.
The development bridge seals every successful Session as reusable transcript evidence.
Evolution is started separately from the Evolution workspace: the user selects one or
more completed Sessions plus compatible legacy-worker methods, and the bridge rejects
unknown target/method pairs or undeclared outputs. A
selection value such as `agent_system.method=auto` is resolved through the Core-owned
method-selection boundary; the daemon records and invokes only the resulting concrete
method. Input artifacts are assembled from the method descriptor's binding sources, so
history methods receive exactly the selected ordered datasets without method-ID branches
in Desktop or the daemon. Outputs remain reviewable candidates and are excluded from
runtime context until the user explicitly applies the Evolution Run. The
server runs `scripts/dev/live_agent_daemon.py` on `127.0.0.1:8787`; an SSH local
forward exposes it as local `127.0.0.1:8765`; and Vite injects the bearer token
while proxying `/openevo-dev-agent/*`. The renderer never receives the token,
SSH command, or remote address. The development daemon stores Projects, active
Project selection, Session state, instructions, Codex responses, model label,
duration, errors, process-log summaries, standalone Evolution Runs, their selected
Session evidence, per-target method/config/job state/artifact IDs, and versioned typed artifacts in
the remote SQLite database `~/.openevo/dev-agent/state.sqlite3`. Dataset and
generated artifact payloads live under
`~/.openevo/dev-agent/evolution-artifacts/`. Before the next Project Session, the
development daemon runs the selected artifacts through the Core target-handler and
contribution contracts. Text memory becomes the handler's bounded instruction
contribution; skill bundles are staged under the isolated runtime workspace's
`.agents/skills/<artifact>/`; and agent-system targets are staged as native harness
instruction files such as `AGENTS.md`. Runtime paths are supplied through the
handler-declared `OPENEVO_*` environment bindings. The temporary runtime projection
also stages an explicit Core-owned runtime-control v1 contract for memory, skill,
or agent-system when the artifact provides one. Existing artifacts default to the
current behavior without gaining an extra runtime contribution. A future Core
method can instead declare on-demand memory reads or a bounded structured agent
spawn plan in `manifest.runtime_control` without adding method IDs or method tables
to Desktop/daemon. Before harness execution, the daemon-owned versioned translator
registry converts each Core control into stable desired-state feature intents. The
Codex adapter reconciles those intents against an explicit capability set and records
every decision as `active`, `delegated`, or `unsupported` in the persisted Session
runtime-activation report. Native memory-at-session-start, harness instruction, and
skill loading work today; session-closed memory writes are delegated to the development
evolution runner. On-demand memory, manual writes, and structured spawn plans remain
explicitly unsupported until a verified executor is installed. Unknown control kinds
or versions fail closed rather than being treated as Markdown or imperative commands.
Adding a future Core contract, such as a bounded tool policy, requires one translator
and corresponding harness capabilities; it does not add algorithm-specific dispatch
to the daemon.
The development adapter also adds the required `name` and `description` frontmatter
to a runtime-only `SKILL.md` copy when an older artifact lacks it; the authoritative
evolved artifact is not rewritten. The temporary runtime projection
is discarded after the turn, while the authoritative artifacts and user workspace
remain persisted on the remote server. Only method outputs marked promoted participate
in the next Session; candidate/report outputs remain visible without being injected. This
development materializer is still explicitly unverified and does not claim the sealed
release artifact-store contract. The renderer
reloads that authority from `GET /openevo-dev-agent/v1/state`, so a browser
refresh preserves Project, conversation, selection, and evolution history. This bridge is
reachable only in Vite mode `openevo-live-agent` and is not imported by the
packaged release entrypoint.

Each development Project also owns a persistent scratch workspace at
`~/.openevo/dev-agent/workspaces/<project-id>/`. A new Project starts with an
empty directory. The daemon gives Codex a bounded text projection of that
workspace and asks for a structured file-mutation plan. The daemon then validates
relative paths, reserved directories, mutation counts, and UTF-8 byte limits
before atomically writing the files. This brokered path also works on remote
servers where the Codex CLI cannot start its Linux `bwrap` sandbox; Codex never
receives unrestricted host filesystem access. Later Sessions in the same Project
see the retained files. The state response exposes
only a bounded, no-symlink readable projection: at most 1,000 entries, 256 KiB
per previewed text file, and 2 MiB of text in aggregate. Host paths are never
returned. Per-Session created, modified, and deleted files are persisted in
SQLite as change summaries and shown under Session output files, while file
bodies remain authoritative on the remote server filesystem.

The real-agent development provider also supports authenticated binary file transfer for
that same workspace. Desktop uploads selected local files with an atomic replace operation
(`PUT /projects/<id>/workspace/files?path=...`) and downloads bounded regular files through
the matching `GET` endpoint. Paths are confined to the managed project root, reserved and
symlinked paths are rejected, uploads are limited to 32 MiB per file and 512 MiB per
workspace, and downloads are limited to 64 MiB. Uploaded files are immediately visible in
the Project files panel and become part of the next Session's persistent workspace.
The daemon projects supported document formats into bounded text without giving Codex arbitrary
host filesystem tools. Text-based PDFs, DOCX, PPTX, XLSX/XLSM, and safe ZIP listings are supported.
PNG, JPEG, WebP, and GIF files are attached through the Codex image-input interface (up to 8 images,
10 MiB each, and 32 MiB total). Image-only PDFs still require OCR. New document formats plug into
the workspace projection layer without changing the Desktop or Session orchestration.

The renderer has no development method table. It builds the optional multi-target
Session picker from the returned capabilities, preserves the selected method and
user config, and renders artifact bodies by `renderer_kind`: Markdown documents,
file bundles, structured reports, or adapter metadata. Adding a future method to
the Core registry therefore does not require another target-ID branch in the
Desktop picker or development daemon dispatch. Methods using an invocation ABI the
development bridge does not yet own remain unadvertised there instead of silently
falling back.

The same development bridge has an SSH-alias-driven one-command launcher. Add
the server once to the local system OpenSSH configuration, then run this from
`desktop/`:

```bash
npm run dev:agent:remote -- --ssh-alias openevo-lab
```

For a source-development server that is not present in `~/.ssh/config`, the
launcher also accepts a validated host, user, and port directly:

```bash
npm run dev:agent:remote -- \
  --host js4.blockelite.cn \
  --user root \
  --ssh-port 27104
```

These direct connection values remain local launcher arguments and never enter
the React renderer or packaged Desktop contract. The release UI continues to
use only system OpenSSH aliases discovered by the native sidecar.

The launcher derives the credential-free GitHub repository URL from local
`origin` and the branch from the current checkout. `--repository-url` and
`--branch` may override those values. The local checkout must be clean and its
exact HEAD must already be present on that fork branch, preventing an apparently
successful deployment from silently running stale code. It creates and exclusively manages
`~/.openevo/dev-agent/source` on the remote host, installs `uv` from its official
installer when absent, syncs Python 3.11 dependencies, restarts the loopback-only
development daemon, verifies its authenticated health endpoint, opens the SSH
local-forward tunnel, and finally starts `dev:agent` with the bearer token kept
in process environment. Closing Vite also closes the tunnel; the remote daemon
continues running and will be safely restarted by the next launcher invocation.
The launcher refuses to overwrite an unrecognized source directory, refuses a
dirty managed checkout, and accepts only a literal SSH alias and a
credential-free GitHub URL. It is a source-development convenience, not a
replacement for the sealed sidecar deployment lifecycle used by packaged
Desktop builds.

The release adapter copies the authenticated
`DesktopStateV1.execution_mode_capabilities` object into
`DesktopProductSnapshot` without projection. Mode tabs, labels, new-project
default selection, and Save/Activate/Start gates consume this single object.
Missing, duplicate, or unknown mode entries fail state parsing; the renderer has
no static support table or fixture fallback. The capability describes shipped
release support and is deliberately separate from remote model/service
observations. Contract simulator scenarios may supply validated alternate states
only in test and Vite preview builds.

The first-run renderer exposes one next action at a time. Until a remote profile
exists, the Research workspace owns the `Add remote workspace` action and project
creation is disabled. Once a profile is present, project creation becomes
available. New-project setup is one recoverable two-stage drawer flow: Desktop
first saves and activates a minimal draft to establish the project tunnel, then
loads that project's remote capabilities and initializes `text_memory`,
`skill_bundle`, and `agent_system` from the remote effective defaults. The
drawer stays open for review and the second save validates and activates the
configured draft. `ProjectV1.evolution_configuration_state` is the durable
authority for this stage: only `pending` reopens or blocks a run. The three
targets are independent switches and the explicit second save may configure all
of them off; `configured` with an empty target map remains complete and runnable.
Older projects migrate deterministically to `configured`.

`LocalApiDesktopProductProvider` is the release adapter. It aggregates all
bounded cursor pages, reloads exact run details, and marks artifacts complete
only when every run artifact page succeeds. Capabilities and validation are
read only for the authoritative active project over its ready tunnel. Native
state, profiles, and projects are loaded before project-bound Core collections;
runs and services are requested only when that same active project reports a
ready, compatible tunnel. A fresh install, draft project, offline server, or
activation in progress therefore remains a usable Local UI with empty remote
collections instead of turning the expected Core 503 into a failed whole-app
refresh. Once the tunnel becomes ready, the next authoritative refresh loads
those collections normally. Native
folder and credential operations remain native-host calls whose results are
strictly parsed as `ProjectSourceV1` and `RemoteProfileV1`; renderer file inputs,
raw paths, and secret values are not accepted.

Run output is loaded on demand through the frozen run-log route rather than
stored in the global renderer snapshot. The provider applies the same bounded
pagination rules and rejects cross-wired run, attempt, service, duplicate, or
non-monotonic log identities. Renderer request state is tagged with separate
opaque run ID and nullable current-attempt ID fields, without delimiter or
sentinel encoding. A transition renders an empty loading state until its own
request resolves, and superseded requests cannot publish into the new identity.
Failed-session recovery calls `retryRun` with the failed run's current ETag,
terminal attempt ID, and a stable action ID. The terminal attempt ID comes from
the durable renderer journal and remains byte-for-byte stable on every replay;
the sidecar never substitutes a newer attempt observed from Core. While that
request is pending, Start and every recovery control are disabled. The returned
object is contract-validated by the provider, and a response that proves one
appended attempt is authoritative even when the immediately following aggregate
snapshot omits or lags that run. An ambiguous request remains visibly failed,
presents the typed error, and retains the complete original action ID, stream
epoch, ETag, terminal attempt ID, and run snapshot for exact replay while the
same run has not proved advancement; ETag churn alone does not replace that
intent. A typed deterministic rejection clears the journal. The release provider
admits only one unresolved retry intent at a time; the first caller claims that
authority synchronously, before its first asynchronous write, so a concurrent
caller cannot cross the persistence boundary. A retry for another run or with
another action ID cannot overwrite it. Before transport, the intent is atomically
written and fsynced by the Tauri native host, not WebView storage. Native access
is serialized by both an in-process mutex and a bounded kernel lock on a private,
identity-verified persistent lockfile, so separate Desktop processes cannot
overwrite the journal. Every write is also a compare-and-swap against the exact
previous journal value observed at startup or after the prior write. A second
Desktop process with stale authority is rejected without changing the first
process's record. If a native write returns an error, the renderer rereads the
verified journal only to classify local state. Unchanged prior bytes preserve the
deterministic local failure. Any changed value, including the exact requested
value, poisons further reads and writes until application restart because
visibility does not prove the directory fsync completed. Invalid or unreadable
journal or lock state fails startup closed and remains available for diagnosis.
Any recovery-store write failure also latches the provider unavailable for the
rest of its lifetime. Every Core retry transport, including a cached exact
replay, checks that latch; persisting an accepted response or clearing a rejected
retry therefore cannot be bypassed without restarting Desktop.
Recovery writes and retry dispatch also share a synchronous provider gate. A
refresh-owned reconciliation clear cannot overlap an exact replay, and an active
retry claim prevents refresh from starting a clear. Once the latch is set, the
provider refuses to expose its stale cached recovery record; the renderer keeps
the restart-required error across fresh or failed refreshes and stops polling.
Bounded polling starts only
after the request becomes ambiguous. An aggregate that arrives while the retry
transport is still in flight cannot reconcile or clear the journal. A later
fresh snapshot reconciles the pending retry only when it preserves the canonical
complete original attempt prefix and contains exactly one appended current
attempt. That reconciliation clears only the error owned by the retry; errors
from newer or concurrent actions remain owned by those actions. A direct 2xx
must also prove the exact Core retry admission reset: the new attempt is queued,
the prior admission pin and timestamps are cleared, and no error remains. Once a
2xx has been journaled, every exact replay is bound to that same appended attempt.
The Research view renders at most the latest 200
matching records and separates agent, evolution, and system streams; SSE
snapshot epochs trigger an authoritative output refresh while a session runs.
The renderer also performs serial one-shot authoritative snapshot refreshes at
a short interval while the selected ready project has a nonterminal run. Each
poll is bound to the provider, Desktop/Core project identities, active project
session, and run ID; a project or session change rejects a late response. The
next timeout is armed only after the current request settles, transient failures
remain retryable, and terminal, offline, switched-project, and unmounted states
stop polling. Poll, SSE, manual, lifecycle, and mutation-dependent refreshes all
use one serial renderer owner. Every request receives a monotonic watermark.
Calls above the running batch's dispatch watermark coalesce into one bounded
trailing refresh; they never capture or delay waiters already owned by the
running batch. A fresh completion is published immediately and resolves its
mutation-dependent waiters before a higher-watermark tail marks that snapshot
stale. Within a coalesced batch, waiter completion priority is mutation,
reconciliation, manual, lifecycle, SSE, then poll. A completion whose polling
identity is no longer current is not rendered; the renderer adopts its
authoritative epoch as stale and performs a serial reconciliation before
mutations are enabled again. SSE invalidations remain the immediate refresh path
without creating parallel snapshot loads.

The release adapter deliberately has no fallback for those native calls. The
Rust host implements `select_project_source` with the operating-system folder
picker. It canonicalizes the selected directory, records its device and inode,
and sends the path plus that identity only through the authenticated private
loopback route to the process-owned sidecar. The sidecar reopens the
identity-bound directory with no-follow traversal and builds the canonical
archive before returning a validated opaque workspace-import reference. The
private response also contains a pending lease token retained only by Rust;
React receives the source and later settles it by non-secret action ID. Drawer
close and source invalidation also send that action ID to a native cancel command.
Rust binds cancellation to a private random token, promptly releases the picker
claim, and lets Python stop traversal, archive, and store work at bounded
checkpoints. A lease published concurrently with cancellation is retained by Rust
before guarded discard, so the renderer never owns recovery authority. Source
replacement, reset, stale completion, and failed save paths discard, while a
successful create/patch settles as adopted only after the sidecar has durably
committed the project reference. Only the opaque source reference enters the
renderer DTO or public Desktop Local API. The release UI exposes only SSH agent
authentication. Historical password/private-key values are shown as unavailable
and save only by switching to SSH agent. No credential prompt, key picker,
credential slot mutation, Keychain account, or secret handoff is exposed to
React. Authenticated proxy and Hugging Face token slots remain reserved and
unavailable; proxy URLs without user-info remain supported.

New projects default to the first release-supported mode, currently the
Core-owned Codex subscription transcript profile and its release-tested
`gpt-5.5` model default. They save an empty evolution target
map until the created and activated project has remote capabilities for its own
identity and execution mode; another project or mode can never provide defaults.
This empty map is an intermediate persisted draft, not a completed user choice.
Failures leave the same draft and mutation intent recoverable in the open drawer,
and closing then refreshing resumes setup from authoritative project state.
`Self-deployed` remains visible with the exact release reason but is disabled in
the current composition. A saved Self-deployed project is never rewritten: it
remains visible, blocks Save/Activate/Start, and can be switched to Subscription.

The project drawer is keyed by the explicit form identity (`create` or exact
project ID). Changing that identity discards the previous component-local draft
and pending capability UI before rendering the new form. Project-only workspace
sync is not exposed by the release provider. Initial folder selection creates a
real immutable native snapshot used by activation; selecting the folder again
creates the replacement snapshot when project content changes. Both modal
drawers lock background scrolling and restore the opener on close. When the
native folder picker temporarily disables its radio control, keyboard focus is
restored to that control after the picker settles.

The System view exposes connection, authoritative service state, environment
doctor/repair, service restart, redacted diagnostics, and bounded cleanup when
the negotiated release composition publishes `service_control`, `diagnostics`,
and `maintenance`. Missing or partial publication keeps
`systemMaintenanceAvailable=false`, hides every mutation control, and leaves
direct route calls fail closed. No path exposes workspace sync, arbitrary
maintenance scopes, or a remote shell.
Nonterminal local connect/bootstrap/activation operations expose the frozen
Local API cancel action, interrupt their exact transport/process/tunnel
authority, and return to the authoritative disconnected or draft state before
retry. Project doctor and repair cannot claim cancellation of a remote side
effect, so their Local cancel route fails closed and the renderer hides that
action. A ready project with an empty service collection or any non-running
service fails closed at `Start session`; the Preview System view offers
authoritative status refresh and full remote-workspace reconnect instead of
treating the degraded environment as runnable.

`App.tsx` owns the release startup state machine. It does not mount the product
renderer until native bootstrap, Local API negotiation, and provider creation
all succeed. Native transitions are serialized through Tauri: initial startup,
retry, StrictMode supersession, and renderer unmount first complete
`stop_sidecar` before another `begin_sidecar_start` can schedule startup; the
renderer then observes only the published `sidecar_bootstrap_context`. A queued
native-start rejection is rechecked after every context read before that context
can be accepted. Failed or superseded attempts are stopped before the next
transition, so they cannot leave an unowned sidecar or publish/reuse their
session token. A bounded native cleanup failure remains visible as retryable
startup failure.

The Local API release digest is
`26ee1e2b6b25f3297c5c09544a9a10ce95baae233ac4b3de2dc0f72cc32ad3cb`.
The checked-in TypeScript mirror and contract fixtures use that frozen digest.
The product UI and simulator consume the final Local/Core v1 DTOs directly and
construct simulator resources through the same strict Zod schemas as release
responses. They do not use renderer-only compatibility wrappers or legacy
field aliases.

## Provider behavior

Refreshes have fixed page, resource, and concurrency budgets. Cursor cycles,
inconsistent `has_more`, identity mismatches, and contract/authentication errors
fail closed. A 503 or transport failure while reading active-project capability
authority maps to `unavailable`; it never selects a local method table.

Mutations pass renderer action IDs unchanged as idempotency keys and observed
ETags unchanged as `If-Match`. Unknown network outcomes are not replayed. SSE
uses one authenticated `ReadableStream`, bounded frames and reconnect attempts,
monotonic sequence checks, duplicate suppression, gap-triggered reload, cursor
reset on HTTP 410, and `AbortController` cancellation on final unsubscribe.

## Renderer recovery and authority

The renderer treats capability payloads as project-and-execution-mode scoped.
An unavailable payload has an explicit retry action and never falls back to a
local method table. Visible method configuration is rendered from the remote
closed JSON encoded by `config_schema_json`. The editor deterministically
deep-merges the decoded `default_config_json` with the project's partial
override for display and validation, while persisting only the user override.
A target with no effective
remote default can still be re-enabled when it retains a supported explicit
method; an empty or invalid selection requires an effective default. Existing
hidden accepted methods and Core-owned selection resolvers remain distinct
from visible choices.

Run outcomes are rendered from `RunV1.status`, `current_attempt`,
`current_error`, exact revision refs, and `revision_transition`. Queued reasons
and failed run errors remain visible, and recovery appends a new attempt to the
same durable run instead of replacing its identity or rewriting a terminal
attempt. Service rows consume `ServiceV1.id`,
`status`, and `status_message`. The renderer exposes those rows and the Research
model-service projection only when the selected `ProjectV1` exactly matches the
active project's project ID, profile ID, and ETag and that connection is ready.
Selecting project B while A is active, or losing A's tunnel, produces an empty
service view. Services are read-only in this release; the frozen restart route
remains reserved and the release provider returns
`provider_capability_unavailable` if it is called directly. An active project
whose tunnel is lost exposes reconnect recovery; only a retired or draft project
requires activation again. Connection and activation mutations use the local
`LocalOperationV1` lifecycle. HTTP
409, 410, and 412 responses trigger an
authoritative snapshot reload; an expired cursor is reset before reload.
Re-admission is offered only for an allowlisted retryable admission conflict
when the refreshed snapshot has no equivalent active or pending run. Drawer
drafts retain a pending mutation intent after an uncertain response. A profile
intent binds its create/update route, canonical payload, action identity,
stream epoch, and update ETag. An authoritative refresh that returns a profile
matching a pending create proves the create succeeded, so the renderer adopts
the resource and closes the drawer without issuing an update. If no matching
profile appears, an unchanged draft retries the original create intent. Editing
the draft or establishing a new update precondition creates a new
route-appropriate intent. Drafts survive reloads and require confirmation
before Escape, overlay, or close-button dismissal.

First-time project creation and activation are two distinct authoritative
mutations. After create succeeds, the renderer reloads the saved project and
uses that fresh stream epoch and ETag for activation; it never chains activation
with the pre-create renderer snapshot. If activation returns HTTP 409 or 412,
the drawer retains the created project identity and creates a new activation
intent against the refreshed ETag. Retry activates that project instead of
issuing another create; an unknown activation result retains its original
action ID for exact retry.

Editing a saved active project demotes it to a draft and retires its Core
session. After a successful save, the authoritative snapshot has no active
project binding, reports `active_tunnel=false`, and requires activation again
before `Start session` is enabled. Activation owns Core startup for this
`core_not_started` state. The simulator mirrors this
terminal provider state so UI tests cannot accidentally continue on the stale
pre-edit tunnel.

Revision generation is shown only from the authoritative
`ProjectV1.remote.active_revision`. Core-owned runs and artifacts are associated
through `ProjectV1.remote.core_project_id`, never the Desktop-local
`project_id`. Matching pinned, required, predecessor, successor, produced, and
membership revision refs must agree on the complete revision identity. Artifact
fixtures preserve the cross-session boundary: a completed run remains pinned to
its predecessor, its active transition names the next generation as successor,
and only the project head advances. A later session then pins that newly active
revision. Fixture revision references reuse the exact complete project-head
identity, including the manifest digest; a matching generation alone is not
sufficient. Project capabilities likewise derive their evaluated execution mode
from the owning project. The ordinary three-target fixture generates only text
memory, skill, and agent-system artifacts. Generic simulator tests that
exercise the closed `parametric_memory` artifact subtype must opt in with
`includeParametricMemory`; artifact execution-mode and model compatibility are
derived from the owning project rather than hard-coded. The completed preview's
generation-one predecessors are explicit seed artifacts with no run or dataset
attribution. Its completed run owns only generation-two outputs, whose lineage
names both their generation-one parent and source dataset. Artifact
lists use selected artifacts whose `membership_revisions` include that exact
active revision, without excluding any authoritative discriminated-union
subtype, and sort them by `created_at` and then `id`. This includes
`parametric_memory`; multiple selected members for one target remain visible.
Content is rendered only when its artifact ID and subtype match the selection.
Changes additionally bind the current content digest and the complete known
previous artifact identity before rendering the `ArtifactDiffV1.document_changes`
union. The provider marks the collection complete only after all cursor pages
have been aggregated. Partial collections and missing or conflicting revision
or artifact evidence are shown as unknown/unavailable with a refetch action
rather than inferred from list order or a loaded run. The simulator keeps its
Desktop and Core project IDs different by default so product tests exercise the
same ownership boundary as release responses.

## Real-agent development Session lifecycle

The loopback development bridge admits `POST /openevo-dev-agent/v1/sessions`
asynchronously. It returns HTTP 202 with a durable `session_id`; Codex runs on a
daemon-owned background thread and completion seals its transcript dataset without
running Evolution. The
Desktop provider refreshes the persisted remote state while a Session is
`running` or `cancelling`, so transcript, logs, workspace changes, failures and
terminal status survive renderer refreshes. A running Session can be cancelled
through `POST /openevo-dev-agent/v1/sessions/<id>/cancel`.

`POST /openevo-dev-agent/v1/evolution-runs` admits a separate durable Evolution Run
against an explicit set of completed Session IDs and method selections. Every selected
method owns a durable Job plus ordered Attempts. Each
Attempt records its current stage, bounded diagnostic log, typed error code,
error message, timestamps, and produced artifact IDs. A failed method can be
retried independently through
`POST /openevo-dev-agent/v1/evolution-jobs/<job-id>/retry`. The retry reuses the
original Session transcript dataset, ordered history dataset IDs, previous
target artifact, resolved concrete method, and normalized method config; it
does not rerun the Agent or silently adopt newer Project settings. Desktop
polls while that Attempt is active and exposes both failure details and the
complete Attempt history in the Evolution workspace. Successful outputs remain
unapplied candidates. `POST /openevo-dev-agent/v1/evolution-runs/<run-id>/apply`
atomically replaces the active artifact for each produced target; only then can later
Sessions receive those artifacts as runtime context.

Harness mechanics are isolated behind `HarnessAdapter`. The first concrete
implementation is `CodexHarnessAdapter`, which owns runtime preparation,
memory instruction injection, native skill and agent-system staging, runtime
control reconciliation, command construction, cancellable process execution,
and transcript/result collection. Session persistence and orchestration do not
branch on Codex-specific launch details; another harness adds another adapter.
