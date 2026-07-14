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

The current native host does not yet return `DesktopBootstrapContextV1`, and no
release adapter implements the converged high-level run or native source
selection operations. Release startup therefore remains fail closed. Native
folder selection must eventually return only `ProjectSourceV1` with an opaque
`ContentRef`; renderer file inputs and raw filesystem strings are not accepted.

## Renderer recovery and authority

The renderer treats capability payloads as project-and-execution-mode scoped.
An unavailable payload has an explicit retry action and never falls back to a
local method table. Visible method configuration is rendered from the remote
closed JSON schema; a target with no effective remote default cannot be
enabled. Existing hidden accepted methods and Core-owned selection resolvers
remain distinct from visible choices.

Run outcomes are rendered from their typed states. Queued reasons and failed
run errors remain visible, and recovery creates a fresh admission instead of
rewriting a terminal attempt. HTTP 409, 410, and 412 responses trigger an
authoritative snapshot reload; an expired cursor is reset before reload, and
an uncertain mutation is never replayed automatically. Drawer drafts survive
these reloads and require confirmation before Escape, overlay, or close-button
dismissal.

Revision generation is shown only when `ProjectV1.current_revision_id` has a
consistent active revision reference. Artifact lists use selected artifacts
whose explicit revision membership includes that revision, sorted by
`created_at` and then `artifact_id`. Missing or conflicting evidence is shown as
unknown with a refetch action rather than inferred from list order or a loaded
run.
