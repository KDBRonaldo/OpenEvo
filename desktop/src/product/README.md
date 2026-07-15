# Desktop Product Renderer Boundary

The product renderer consumes only `DesktopProductProvider`. Mutations carry the
renderer-observed stream epoch, resource ETag, and a stable action identity.
`startRun` intentionally carries only project identity and intent metadata; the
Local API owner must perform project snapshot, capability, validation, and
revision handshakes.

Release startup has one entry point: `createReleaseDesktopProductProvider`.
It accepts a provider only after the Tauri bootstrap and `DesktopApiClientV1`
agree on contract major, checked-in OpenAPI digest, provider kind, and required
features. The contract simulator is test-only and is not a release fallback.
The `openevo-desktop` Vite mode replaces the general provider-kind parser with
`providerKinds.release.ts`, whose only accepted value is `desktop_sidecar`.
Rollup can then remove simulator, scaffold, and dry-run provider definitions and
their strings from the packaged renderer. Normal development/typecheck/test
imports continue to use `providerKinds.ts`, so contract fixtures remain usable
without becoming release dependencies.

For renderer visual QA, run the Vite development server and open
`/product-preview.html?scenario=<name>`. The closed scenario set is `new-user`,
`offline`, `online`, `completed`, and `degraded`. This secondary HTML entry is
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
diagnostics. Contract simulator scenarios may supply validated alternate states
only in test and Vite preview builds.

The first-run renderer exposes one next action at a time. Until a remote profile
exists, the Research workspace owns the `Add workspace` action and project
creation is disabled. Once a profile is present, project creation becomes
available. New-project setup is one recoverable two-stage drawer flow: Desktop
first saves and activates a minimal draft to establish the project tunnel, then
loads that project's remote capabilities and initializes `text_memory`,
`skill_bundle`, and `agent_system` from the remote effective defaults. The
drawer stays open for review and the second save validates and activates the
configured draft. Refreshing an empty-target prepared draft reopens this stage;
it cannot appear complete or become runnable with an accidental empty map.

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
renderer DTO or public Desktop Local API. The
release UI exposes only SSH agent authentication. Password, private-key, and
proxy-secret credential brokers remain contract extension points, but the
default native bridge rejects those calls without invoking a nonexistent Tauri
command. They must not appear as usable release controls until the native broker
is implemented and reviewed.

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
creates the replacement snapshot when project content changes.

The System view invokes project diagnostics through the active project tunnel
and exposes service restart only for Core services that declare restart support.
It does not show local repair controls because the release provider has no repair
handler. Nonterminal local connect/bootstrap/activation operations expose the
frozen Local API cancel action and return to the authoritative disconnected or
draft state before retry.

`App.tsx` owns the release startup state machine. It does not mount the product
renderer until native bootstrap, Local API negotiation, and provider creation
all succeed. Native transitions are serialized through Tauri: initial startup,
retry, StrictMode supersession, and renderer unmount first complete
`stop_sidecar` before another `start_sidecar` can issue a credential. Failed or
superseded attempts are stopped before the next transition, so they cannot
leave an unowned sidecar or publish/reuse their session token. A bounded native
cleanup failure remains visible as retryable startup failure.

The Local API release digest is
`e3bc443ee213eb33de81b82c7f954fb617fab14b8a2c17e154f3d4b980ba441f`.
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
and failed run errors remain visible, and recovery creates a fresh admission
instead of rewriting a terminal attempt. Service rows consume `ServiceV1.id`,
`status`, and `status_message`. The renderer exposes those rows and the Research
model-service projection only when the selected `ProjectV1` exactly matches the
active project's project ID, profile ID, and ETag and that connection is ready.
Selecting project B while A is active, or losing A's tunnel, produces an empty
service view; restart lookup uses that same gated collection and cannot target
A through B's screen. An active project whose connection is no longer ready
shows activation again so it can establish a new session. Service restart
returns the typed Core `OperationV1`; connection, activation, and workspace
mutations continue to use the separate local `LocalOperationV1` lifecycle. HTTP
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
project binding, reports `active_tunnel=false`, and requires connection and
activation again before `Start session` is enabled. The simulator mirrors this
terminal provider state so UI tests cannot accidentally continue on the stale
pre-edit tunnel.

Revision generation is shown only from the authoritative
`ProjectV1.remote.active_revision`. Core-owned runs and artifacts are associated
through `ProjectV1.remote.core_project_id`, never the Desktop-local
`project_id`. Matching pinned, required, predecessor, successor, produced, and
membership revision refs must agree on the complete revision identity. Artifact
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
