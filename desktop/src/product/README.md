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

The remaining bootstrap-retry integration point is `App.tsx`. It currently
replaces a failed `createReleaseDesktopProductProvider` call with the unavailable
provider. A later startup-state change must expose an explicit retry that calls
the release factory again, obtaining a fresh `start_sidecar` bootstrap context;
it must not retain or retry with a failed session token.

The Local API release digest is
`3a86582d04dcd233096337c737ba91d75854746848aedc319025d86213a03d36`.
The checked-in TypeScript mirror and contract fixtures use that frozen digest.
The existing product UI and simulator fixture still require a separate consumer
migration before the complete product UI suite can be treated as a release gate.

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
closed JSON schema. The editor deterministically deep-merges a method's
`default_config` with the project's partial override for display and
validation, while persisting only the user override. A target with no effective
remote default can still be re-enabled when it retains a supported explicit
method; an empty or invalid selection requires an effective default. Existing
hidden accepted methods and Core-owned selection resolvers remain distinct
from visible choices.

Run outcomes are rendered from their typed states. Queued reasons and failed
run errors remain visible, and recovery creates a fresh admission instead of
rewriting a terminal attempt. HTTP 409, 410, and 412 responses trigger an
authoritative snapshot reload; an expired cursor is reset before reload.
Re-admission is offered only for an allowlisted retryable admission conflict
when the refreshed snapshot has no equivalent active or pending run. Drawer
drafts retain their action identity after an uncertain response. The identity
changes only when draft content changes or a 409/412 refresh establishes a new
request precondition. Drafts survive reloads and require confirmation before
Escape, overlay, or close-button dismissal.

Revision generation is shown only when `ProjectV1.current_revision_id` has a
consistent active revision reference. Artifact lists use selected artifacts
whose explicit revision membership includes that revision, sorted by
`created_at` and then `artifact_id`; multiple selected members for one target
remain visible. The provider marks the collection complete only after all
cursor pages have been aggregated. Partial collections and missing or
conflicting revision evidence are shown as unknown with a refetch action rather
than inferred from list order or a loaded run.
