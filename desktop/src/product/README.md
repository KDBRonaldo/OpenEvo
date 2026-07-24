# Desktop Product Renderer Boundary

## Built-in scientific project demos

The Desktop demonstration surface contains two scientifically distinct,
curated project stories. Neither story represents a remote run or authoritative
project state.

- The renderer owns an enzyme-kinetics tour and a protein-stability evidence
  tour. Both are always listed as `[Demo]` in the real release
  project selector, including before any remote workspace exists and while the
  Desktop sidecar startup fallback is visible.
- `Add remote workspace` remains visible in the top bar throughout startup and
  snapshot recovery. A request made before authority is available is retained,
  recovery is retried, and the real connection drawer opens only after the
  provider has published an authoritative snapshot.
- Each project contains three sessions with a rejected baseline, later
  corrective work, concise transcript activity, and a validated
  condition-scoped result. The protein project uses plate-aware DSF plus
  orthogonal SEC evidence rather than copying the enzyme workflow.
- Both projects advance Project Head generations 0 through 3 and separately
  identify their Evolution Revisions. Every session pins one predecessor and
  makes its successor available to the next session.
- Every project contains `text_memory`, `skill_bundle`, and `agent_system`
  histories with three evolution steps, readable Markdown, and an explicit
  previous-versus-current diff.
- All sample data and rendering live in `scientificProjectSampleData.ts` and
  `ScientificProjectSample.tsx`; samples are not inserted into
  `DesktopProductProvider`, Local API state, Core state, or run admission.
- The deeper session timeline keeps project generations and evolution update
  identities separate; the demonstrations do not collapse the full project
  composition into its evolution artifact set.
- Demonstration interactions issue no mutations and initiate no SSH, Daemon, Core,
  artifact, or external-network work. Normal read-only Desktop-local state
  synchronization remains active so real projects stay discoverable.
- The samples remain available beside real projects, but never replace an
  authoritative project selection or revision.
- `npm run test:product-browser` verifies the first-run Research and Evolution
  views at the closed 1440, 1024, and 760 pixel viewports with Chromium,
  committed visual baselines, keyboard interaction, viewport bounds, and
  serious/critical axe findings.

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
their strings from the packaged renderer. Normal development/typecheck/test
imports continue to use `providerKinds.ts`, so contract fixtures remain usable
without becoming release dependencies.

For renderer visual QA, run the Vite development server and open
`/product-preview.html?scenario=<name>`. The closed scenario set is `new-user`,
`offline`, `online`, `completed`, `degraded`, and `failed`. Except for the empty
`new-user` state, these scenarios use the release-supported
`codex_subscription_transcript` profile with transcript capture and `gpt-5.5`.
The `failed` scenario contains one genuinely failed run and exercises the
same-run retry action; it does not add a successful run to make the history look
healthy. This secondary HTML entry is
served only by Vite during development; the Tauri release build starts from
`index.html`, and `preview.tsx` also rejects production execution. The preview
therefore exercises the real product components against strict contract
fixtures without becoming a release provider or fallback.

Native development uses `npm run tauri:dev`. Tauri starts Vite through
`dev:openevo`, which selects the same product-only entrypoint as the release
build before attaching the native bridge. The plain `npm run dev` command
remains available for the separate shared observability pages; it is not the
Desktop native development surface.

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
