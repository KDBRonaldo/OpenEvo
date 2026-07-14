# Core Control API v1 Provider

This document describes the phase-one business provider for the frozen Core
Control API v1 contract. The canonical HTTP and event schemas remain owned by
`src/openevo/backend/contracts/v1/openapi.json` and `events.schema.json`.

## Construction And Boundary

`create_core_control_contract_app()` remains the schema source. Without a
provider it returns the original HTTP 501 contract-only response on every
route. `create_core_control_app(...)` creates `CoreControlStoreV1`, binds
`CoreControlProviderV1` to the same routes by canonical `operation_id`, and
closes the store during application shutdown. It does not register a second
route table, call Desktop `/openevo-api` routes, or call model APIs.

`GET /version` and `GET /health` are anonymous. Every path with a versioned
major prefix requires exactly one byte-exact `Authorization: Bearer <token>`
header. The bearer is process-owned and is never persisted. `/version` reports
major 1, the canonical OpenAPI digest, build identity, `openevo_core`, and the
implemented feature families. An authenticated request for another `/vN`
major returns typed HTTP 426; there is no legacy fallback.

The frozen strict models previously rejected exact JSON string enum values
after FastAPI decoded a request body to a Python dictionary. Request enum
fields now permit only their exact enum strings at that HTTP boundary. The
OpenAPI and events snapshots and their digests are unchanged.

## Endpoint Ownership

| Endpoint group | Phase-one behavior |
| --- | --- |
| `/version`, `/health`, `/v1/status` | Implemented by the Core provider. Health is not ready and status is degraded when no verified executable registry is installed. |
| `/v1/capabilities` | Direct `build_evolution_capabilities` projection of the injected `VerifiedExecutableRegistry`; missing registry returns typed 503. |
| `/v1/projects` and `/v1/projects/{id}` | Durable list/create/get/patch/delete with strong ETags, conditional mutations, bounded signed cursors, and persisted idempotency responses. |
| `/v1/projects/{id}/workspace-uploads/*` | Durable begin/status/ordered chunk/finalize/abort with project and upload CAS, restart recovery, digest validation, canonical ustar verification, and extracted snapshot publication. |
| `/v1/projects/{id}/validate` | Validates exact current project/workspace snapshots and registry digest, then delegates evolution selection validation to the existing framework compiler validator. |
| `/v1/services`, `/v1/services/{id}` | Reports the observable `core-control` process state. No other service is inferred from files or legacy scaffold state. |
| `/v1/events` | Durable ordered SSE with signed opaque record IDs, at-least-once replay, a 10,000-record maximum window, 15-second durable heartbeats, and typed 400/410 cursor errors. |
| Environment doctor/repair | Typed 503 until a real environment owner and recoverable operation implementation are wired. |
| Revision reads | Typed 503 until successor readiness and activation have a production issuer. |
| Run, timeline, run log, and run context routes | Typed 503. Phase one does not create a run or synthesize admission, attempts, pins, progress, or success. |
| Artifact routes | Typed 503 until run ownership exposes authoritative v1 artifact projections. |
| Service restart/log routes | Typed 503. The provider does not invoke Desktop SSH lifecycle or pretend it supervises services it cannot observe. |
| Operation, referenced-log, diagnostic, and cache-cleanup routes | Typed 503 until their durable business owners are implemented. |

All unavailable operations use `provider_capability_unavailable`; they never
return fixture resources or synthetic successful operations.

## Durable State

The provider owns `<state-root>/core-control-v1/` with an exclusive owner lock:

```text
provider.sqlite3
provider.lock
workspace-uploads/<upload-id>.part
workspace-snapshots/<snapshot-id>/...
```

SQLite stores closed project and upload documents, resource versions,
idempotency responses, a persistent cursor/event signing key, and SSE frames.
Persisted documents are revalidated against their exact v1 models at startup.
The store uses full synchronous commits and WAL journaling. It does not persist
the bearer, host paths in API resources, model credentials, commands, or open
metadata.

Startup recovery runs while the provider holds its exclusive process lock. The
store retains provider-root, owner-lock, upload-root, and snapshot-root FDs for
its full lifetime. Every related operation revalidates each held inode against
its pathname plus the required owner, mode, type, and link count. A provider-root
flock prevents a removed owner-lock pathname or replaced workspace root from
admitting a second owner. Recovery reuses the held owner-verified private root
FDs, validates every project publication against its finalized upload and the
exact published tree, and fails startup when a referenced archive or snapshot
is missing, replaced, or corrupt. Unreferenced upload files, temporary
publications, and snapshots left by a crash after publish rename but before the
SQLite commit are removed relative to those held FDs without following
symlinks; cleanup never traverses outside the managed roots.

Project and upload ETags hash the complete validated canonical resource model,
including model-populated defaults, plus an internal monotonic resource version.
The same canonical model is persisted and used by startup recovery. Idempotency
identity binds the Core v1 principal, operation ID, resource scope, canonical
request, and semantic CAS headers. An exact replay returns the original typed
success or error response and ETag; a conflicting request returns
`idempotency_key_reused`.

Project creation signs Core-owned project and task snapshots. Scratch projects
also receive an immutable empty workspace snapshot. Imported projects remain
draft until their declared archive is finalized. The provider does not invent
an active revision, so projects cannot become `ready` in this phase.

## Workspace Publication

Upload creation freezes the current project snapshot, project ETag, archive
declaration, and optional base workspace snapshot. A chunk is accepted only at
`accepted_offset`, is limited by the frozen 8 MiB chunk contract, and must match
its decoded length and SHA-256. Core loops over short writes and treats a zero
write as failure; the held upload FD is truncated back to the previous durable
offset on any write, `fsync`, or database failure. SQLite advances the offset
only after every byte is present and `fsync`ed. On startup, an uncommitted file
tail is truncated to the durable offset; a file shorter than that offset fails
closed.

Finalize requires both upload `If-Match` and `If-Project-Match`. The latter must
equal the upload's frozen project ETag and the current project ETag, and the
frozen project snapshot must still be current. Core verifies complete size and
SHA-256, private archive mode `0600`, every deterministic POSIX ustar header and
checksum, NFC POSIX paths, parent/order rules, modes, entry and extracted-byte
totals, zero padding, and the exact two-block terminator. Symlinks, hardlinks,
devices, extensions,
sparse/out-of-order content, root or embedded `.`/`..` path segments, and
trailing bytes are rejected as typed `workspace_archive_invalid` conflicts.

Archive parsing hashes the exact bytes used to create the snapshot and then
rehashes the same held file while rechecking its inode and metadata before any
rename. Same-inode, same-size mutation therefore fails closed. Verified files
are written descriptor-relative under a private temporary snapshot directory,
`fsync`ed, and renamed relative to the held snapshot-root FD. A final SQLite
transaction rechecks both mutable resources, stores one `WorkspacePublicationV1`,
signs a new project snapshot, updates the project and upload, and appends the
project event. Recovery verifies the published owner, modes, link counts, exact
entry set, sizes, and bytes against the retained canonical archive before
serving projects. No run consumes this snapshot until the later run-owner phase.

## SSE Recovery

Every stored frame is validated as `SseFrameV1`; wire `id` and `event` therefore
match the typed envelope. Project mutations emit `project.updated.v1` with the
authoritative project ETag. Frame sequence is monotonic and the opaque ID is
authenticated by the store key. A retained `Last-Event-ID` resumes after that
sequence. A valid cursor older than the window returns 410 so the sidecar
reloads snapshots; malformed, tampered, or future cursors return 400.

The stream polls durable state, emits retained records in order, and persists a
heartbeat after 15 seconds without another frame. Restarting Core preserves the
same cursor key and replay order.

## Verification

Provider coverage is in
`tests/backend/test_core_control_v1_provider.py`. The frozen contract tests in
`tests/backend/test_contract_v1.py` continue to prove exact OpenAPI and event
schema bytes and digests.
