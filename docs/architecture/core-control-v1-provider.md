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

Provider dispatch runs synchronous SQLite and workspace I/O on a dedicated
four-worker executor. The ASGI event loop remains available for health checks
and SSE delivery while an archive operation or transaction is blocked. SSE
polling uses the same bounded executor, and shutdown closes the store there
before releasing the threads.

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
idempotency responses, publication-owner audit bindings, pending managed-file
cleanup intents, a persistent cursor/event signing key, and SSE frames.
Persisted documents are revalidated against their exact v1 models at startup.
The store uses full synchronous commits and WAL journaling. It does not persist
the bearer, host paths in API resources, model credentials, commands, or open
metadata.

This pre-release provider state is explicitly **fresh-only** after the review
hardening schema change. Startup creates the current schema for an empty
database and compares every `sqlite_schema` row with a schema built from the
same checked-in DDL. Earlier phase-one state, near-match DDL, extra
table/index/view/trigger, or partially altered schema is rejected. Core does
not infer or backfill missing idempotency request envelopes.

Startup recovery runs while the provider holds its exclusive process lock. The
store retains provider-root, owner-lock, upload-root, and snapshot-root FDs for
its full lifetime. Every related operation revalidates each held inode against
its pathname plus the required owner, mode, type, and link count. Before it
creates or opens the provider root, the store holds and exclusively locks the
stable state-parent directory inode. The provider-root and owner-lock locks are
additional bindings, but replacement of the complete canonical provider root
cannot admit a second owner while the original parent-anchored owner is alive.
Recovery reuses the held owner-verified private root FDs, validates every project
publication against its finalized upload and the exact published tree, and fails
startup when a referenced archive or snapshot is missing, replaced, or corrupt.
Each publication identity, workspace snapshot ID, and content ID is bound to
exactly one project/upload pair by a signed private owner row; a second
persisted owner is corruption regardless of row order. Owner rows remain only
while a live upload or retained idempotency response needs the audit binding.

After validating database authority and deriving the complete live upload and
snapshot name sets, startup reconciles unreferenced upload files, temporary
publications, quarantine entries, and snapshots left by a crash. One cumulative
node/name-byte budget covers both managed roots and every recursive cleanup;
budget exhaustion leaves the remaining orphan for a later startup or exact
operation replay but does not count it as live quota. Managed disk quota is
then evaluated only over database-owned live entries. Unsafe or unrecognized
entry metadata still fails closed.

The main SQLite database authority FD, WAL, and SHM are opened no-follow
relative to the held provider-root FD and retained for the store lifetime.
Python sqlite has no native attach-existing-FD API, so on Linux Core opens the
main connection through `/proc/self/fd/<authority-fd>`, verifies SQLite's
resolved `main` path, and rechecks the held root, pathname, and inode around
connection setup. A transient attach race is retried once; missing `/proc`
support, a repeated race, or any binding mismatch fails startup closed. The
provider and root locks are part of this protocol; SQLite itself is not claimed
to honor those advisory locks. Each database file must remain an owner-owned,
link-count-one `0600` regular file at the same pathname and inode.
Startup runs bounded `integrity_check(1)` and `foreign_key_check`, applies an
exact page limit, and rejects database, WAL, startup-row, aggregate-blob,
managed-entry, and managed-disk quota violations. Recovery reads tables in
fixed-size rowid keyset pages: every persisted TEXT or BLOB value is included in
the SQL aggregate and per-value length budgets before a guarded value enters
Python. Metadata is a one-key closed set; signing-key key/value lengths are
checked before the value is read, and unknown metadata fails startup. Provider
recovery and project listing do not use unbounded `fetchall()`.

Project and upload ETags hash the complete validated canonical resource model,
including model-populated defaults, plus an internal monotonic resource version.
The same canonical model is persisted and used by startup recovery. Idempotency
identity binds the Core v1 principal, operation ID, resource scope, canonical
request envelope, and semantic CAS headers. Success rows persist the canonical
request/header bytes as well as their digest. An exact replay returns the
original typed
success or error response and ETag; a conflicting request returns
`idempotency_key_reused`. Startup validates every persisted success before it
reconciles failed-request audit rows. If legacy or corrupt state contains both
outcomes for one operation, resource, and key, a valid canonical success is
authoritative and the duplicate failed row is removed. An invalid success fails
startup before that cleanup can commit. Success validation is operation-specific
and closed: status and response type must match the operation; project creation
uses only the global `projects` scope; project mutations and validation use a
Core-generated project scope; upload chunk, finalize, and abort scopes bind both
the parent project and upload IDs. Returned project/upload IDs, parent project,
managed snapshot identities, upload lifecycle status, finalize publication, and
the provider-owned validation result semantics must all match that scope and
operation. Create, patch, upload-create, chunk, finalize, abort, and validation
also revalidate request-to-response relations. Validation binds the requested
registry and snapshots, chunks bind the requested final offset, and finalize
binds both CAS headers and the requested archive digest. Replay and startup use
the same validator. A canonical response from another resource is therefore
corruption, not an authoritative success.

Project creation signs Core-owned project and task snapshots. Scratch projects
also receive an immutable empty workspace snapshot. Imported projects remain
draft until their declared archive is finalized. The provider does not invent
an active revision, so projects cannot become `ready` in this phase.
Snapshot and publication content IDs are deterministic HMAC identities over
their canonical digest, timestamp, publication fields, and, for imported
workspace publications, the exact project/upload owner pair. Startup recomputes
task, project, scratch-workspace, published-workspace, and content identities;
project/upload rows also carry a signing-key-bound identity HMAC.
Project validation constructs the framework execution profile from the exact
persisted execution mode, capture mode, and harness ID; it does not substitute
the capability endpoint's release-mode Codex/transcript projection.

## Workspace Publication

Upload creation freezes the current project snapshot, project ETag, archive
declaration, and optional base workspace snapshot. A chunk is accepted only at
`accepted_offset`, is limited by the frozen 8 MiB chunk contract, and must match
its decoded length and SHA-256. Core loops over short writes and treats a zero
write as failure; the held upload FD is truncated back to the previous durable
offset on any write, `fsync`, or pre-commit database failure. SQLite advances
the offset only after every byte is present and `fsync`ed. A lifecycle failure
after `COMMIT` still fails the request closed, but cannot truncate bytes or
rewrite the offset/idempotency result that already committed. An exception at
the SQLite `COMMIT` boundary is treated as an unknown outcome. Core opens a new
connection and verifies, in one read snapshot, either the exact prior upload
row with no success record or the exact new upload and success rows; it also
revalidates the held upload inode, size, and written chunk bytes. Durable
success is returned without recording a failed idempotency result or truncating
the archive. Only a proven rollback may restore the old file offset; mixed or
unverifiable state fails closed while preserving the bytes. On startup, an
uncommitted file tail is truncated to the durable offset; a file shorter than
that offset fails closed.

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
are written descriptor-relative under a private temporary snapshot directory
and `fsync`ed. Linux `renameat2(RENAME_NOREPLACE)` publishes the owner-scoped
snapshot without replacing an existing name. A final SQLite transaction
rechecks both mutable resources, inserts the unique signed project/upload owner
row, stores one `WorkspacePublicationV1`, signs a new project snapshot, updates
the project and upload, and appends the project event. Recovery verifies the
published owner, modes, link counts, exact entry set, sizes, and bytes against
the retained canonical archive before serving projects. No run consumes this
snapshot until the later run-owner phase.
Recovery additionally requires the finalized source upload to belong to the
same project that references the publication.

Upload creation retains the new file descriptor until the transaction outcome
is known and cleans it only after a proven rollback. Finalize retains an inode
receipt for the published snapshot and applies the same rule to the publication
transaction; unknown or committed outcomes preserve the tree for startup
recovery. Cleanup first revalidates the observed inode, atomically renames it to
a random no-replace quarantine name, `fsync`s the owning directory, revalidates
the quarantine binding, and only then unlinks or recursively removes that
quarantine name. It never verifies one inode and deletes the original pathname
after a replacement race. Failed extraction cleanup likewise passes the first
observed temporary-root identity; a same-owner pathname replacement is preserved
without traversal or name-based deletion for authoritative startup recovery.

Abort and project-delete transactions persist signed cleanup intents tied by a
foreign key to their successful idempotency record. Post-commit cleanup removes
an intent only after a complete bounded reconciliation. Cleanup failure returns
a post-commit store error while preserving both the success and intent; an exact
same-key replay must retry reconciliation before returning that success, and
startup uses the same convergent protocol. Aborted uploads no longer reserve
managed archive bytes. This protocol requires Linux `renameat2`; unsupported
platforms fail closed rather than falling back to a replacing rename.

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
Startup and replay require canonical frame bytes and authenticate each stored
frame ID against its exact row sequence. A shape-valid but unsigned persisted
cursor is store corruption.

Typed conflict metadata is code-specific. Snapshot and registry races are
retryable after an authoritative reload; malformed archive or config requests
are non-retryable and `openevo_can_reconfigure`; storage quota exhaustion
requires user action; detected durable-state corruption is non-retryable.

## Verification

Provider coverage is in
`tests/backend/test_core_control_v1_provider.py`. The frozen contract tests in
`tests/backend/test_contract_v1.py` continue to prove exact OpenAPI and event
schema bytes and digests.
