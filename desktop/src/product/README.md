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

`LocalApiDesktopProductProvider` is the release adapter. It aggregates all
bounded cursor pages, reloads exact run details, and marks artifacts complete
only when every run artifact page succeeds. Capabilities and validation are
read only for the authoritative active project over its ready tunnel. Native
folder and credential operations remain native-host calls whose results are
strictly parsed as `ProjectSourceV1` and `RemoteProfileV1`; renderer file inputs,
raw paths, and secret values are not accepted.

The release adapter deliberately has no fallback for those native calls. The
Rust host implements `select_project_source` with the operating-system folder
picker. It canonicalizes the selected directory, sends the path only over an
authenticated private loopback route to the process-owned sidecar, and returns
only a validated opaque workspace-import reference to the renderer. The path is
never part of the renderer DTO or the public Desktop Local API. The remaining
`configure_credential` command is still a required release integration
dependency; the provider continues to fail closed until it is implemented.

The remaining bootstrap-retry integration point is `App.tsx`. It currently
replaces a failed `createReleaseDesktopProductProvider` call with the unavailable
provider. A later startup-state change must expose an explicit retry that calls
the release factory again, obtaining a fresh `start_sidecar` bootstrap context;
it must not retain or retry with a failed session token.

The Local API release digest is
`3a86582d04dcd233096337c737ba91d75854746848aedc319025d86213a03d36`.
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
`status`, and `status_message`. Service restart returns the typed Core
`OperationV1`; connection, activation, and workspace mutations continue to use
the separate local `LocalOperationV1` lifecycle. HTTP 409, 410, and 412 responses trigger an
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
