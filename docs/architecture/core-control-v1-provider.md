# Core Control API v1 Provider

This document describes the phase-one business provider for the frozen Core
Control API v1 contract. The canonical HTTP and event schemas remain owned by
`src/openevo/backend/contracts/v1/openapi.json` and `events.schema.json`.

## Construction And Boundary

`create_core_control_contract_app()` remains the schema source. Without a
provider it returns the original HTTP 501 contract-only response on every
route. `create_core_control_app(...)` creates `CoreControlStoreV1`, binds
`CoreControlProviderV1` to the same routes by canonical `operation_id`, and
constructs the release run owner through a factory that receives that exact
store. Shutdown first asks the run owner to stop, closes managed services to
release in-flight I/O, joins the owner, and finally closes the store. It does not register a second
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
| Revision reads | Durable project-owned active revision ledger. List uses bounded signed cursors; head and revision reads return their authoritative ETags. |
| `/v1/projects/{id}/workspace-uploads/*` | Durable begin/status/ordered chunk/finalize/abort with project and upload CAS, restart recovery, digest validation, canonical ustar verification, and extracted snapshot publication. |
| `/v1/projects/{id}/validate` | Validates exact current project/workspace snapshots and registry digest, then delegates evolution selection validation to the existing framework compiler validator. |
| `/v1/services`, `/v1/services/{id}` | Reports `core-control` plus read-only summaries from the release-injected `CoreServiceSupervisor`. No service is inferred from files, Desktop commands, or legacy scaffold state. |
| `/v1/events` | Durable ordered SSE with signed opaque record IDs, at-least-once replay, a 10,000-record maximum window, 15-second durable heartbeats, and typed 400/410 cursor errors. |
| Environment doctor/repair | Typed 503 until a real environment owner and recoverable operation implementation are wired. |
| Run, timeline, run log, run context, and run artifact-list routes | The release launcher injects `CoreScienceRunOwner`, which durably executes and observes the complete frozen `/v1/runs*` family. Tests may omit the owner and receive typed 503. |
| Standalone artifact routes | Typed 503 until run ownership exposes authoritative v1 artifact projections. |
| Service restart/log routes | Typed 503 until durable operation ownership is implemented. The provider never invokes Desktop SSH lifecycle or infers services it cannot observe. |
| Operation, referenced-log, diagnostic, and cache-cleanup routes | Typed 503 until their durable business owners are implemented. |

All unavailable operations use `provider_capability_unavailable`; they never
return fixture resources or synthetic successful operations.

The repository contains an opt-in maintenance-owner prototype for focused
contract tests. Production and release construction do not instantiate it,
do not advertise `DIAGNOSTICS`, and keep every owner-backed route fail closed
until durable side-effect receipts, lifecycle/replay semantics, run fencing,
shutdown draining, and complete authority audits are implemented and reviewed.

## Durable State

The provider owns `<state-root>/core-control-v1/` with an exclusive owner lock:

```text
provider.sqlite3
provider.lock
provider.identity
workspace-uploads/<upload-id>.part
workspace-snapshots/<snapshot-id>/...
```

SQLite stores closed project, active revision, and upload documents, resource versions,
idempotency responses, publication-owner audit bindings, pending managed-file
cleanup intents, a persistent cursor/event signing key, and SSE frames.
Persisted documents are revalidated against their exact v1 models at startup.
The store uses full synchronous commits and WAL journaling. It does not persist
the bearer, host paths in API resources, model credentials, commands, or open
metadata.

The private schema is exact-fingerprinted. Startup accepts only an empty
database, the current schema, either exact preceding provider schema, or the
older empty pre-identity schema.
Near-match DDL, extra tables/indexes/views/triggers, and partially altered
schemas are rejected. The exact preceding bound provider schema is upgraded in
one SQLite transaction by adding the empty revision ledger. The immediately
preceding ledger layout is upgraded transactionally with the private activation
request binding; a retained activation response is then validated before it may
backfill the ledger and idempotency-owned audit bindings. Existing projects,
uploads, events, and idempotency
records remain authoritative and are checked by the normal bounded recovery
pass. Projects from the pre-ledger schema stay draft until a later verified
mutation can publish a revision; startup does not infer readiness or synthesize
events. The older pre-identity schema can migrate only
when every business table and both managed roots are empty and no identity
marker exists. Core does not infer or backfill missing idempotency request
envelopes.

`store_identity` contains one closed row with a random `store_id`, the bound
provider-root device/inode, a `pending|bound` state, and, once bound, the marker
device/inode. `provider.identity` is canonical JSON containing that same
`store_id` and provider-root identity. It is opened no-follow, retained by FD,
and must remain an owner-owned, link-count-one `0600` regular file at the same
pathname and inode. Initial creation and the allowed empty legacy migration use
three durable phases: one SQLite transaction creates the complete schema and
the `pending` identity row; a random private temporary marker is completely
written and fsynced, atomically published with no-replace rename, and followed
by a provider-root fsync; a second SQLite transaction records the published
marker inode and changes the row to `bound`. Before any identity row is created,
Core creates or opens both canonical managed roots, fixes their private directory
inodes by FD, and verifies an empty immediate inventory. The same held FDs and
pathname identities are rechecked after durable marker publication and again
inside the final `pending -> bound` transaction. A crash with a pending row and
no marker republishes it; a pending row with the exact published marker repeats
the same managed-root checks before completing the second transaction. Any new
node or root replacement fails closed while the identity remains pending, before
orphan reconciliation can run. Unpublished temporary marker inodes are
conservatively retained instead of deleted by pathname.

Startup recovery runs while the provider holds its exclusive process lock. The
store retains provider-root, identity-marker, owner-lock, upload-root, and
snapshot-root FDs for its full lifetime. Every related operation revalidates
each held inode against its pathname plus the required owner, mode, type, and
link count. Before it creates or opens the provider root, the store holds and
exclusively locks the stable state-parent directory inode. The provider-root and
owner-lock locks are additional bindings, but replacement of the complete
canonical provider root cannot admit a second owner while the original
parent-anchored owner is alive. For a current bound store, startup attaches the
held database authority and verifies the DB/root/marker identity before it
creates or opens either managed root, enables WAL, traverses workspace state, or
performs recovery mutation. Fresh and legacy bootstrap may descriptor-open an
existing managed root, or create a missing canonical root, only to prove through
the continuously held FD that its immediate inventory remains empty through the
identity bind; it does not traverse children or clean entries.
Copying a legitimate `provider.sqlite3` to another root, swapping markers,
removing a bound marker, or presenting a fresh database beside existing managed
state therefore fails closed without orphan cleanup. Recovery then reuses the
held owner-verified private root FDs, validates every project publication against
its finalized upload and the exact published tree, and fails startup when a
referenced archive or snapshot is missing, replaced, or corrupt.
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

The main SQLite database authority FD and every pre-existing rollback journal,
WAL, and SHM sidecar are opened no-follow relative to the held provider-root FD
before SQLite connects and are retained for the store lifetime. Each starts as
an owner-owned, link-count-one `0600` regular file at the exact canonical
pathname. After hot-journal recovery, the original rollback-journal inode must
either remain bound there or be the now-unlinked inode SQLite consumed while the
canonical pathname remains absent. A replacement pathname, unsafe original
inode, or ambiguous consumption fails closed. After every explicit SQLite
`COMMIT` or `ROLLBACK` boundary, including a boundary whose result is reported
as unknown, Core reconciles that held rollback-journal inode before general
lifecycle authority verification. The held state may only remain bound to the
same pathname or advance once to consumed; a consumed inode cannot be rebound.
Journal, WAL, and main-database byte budgets are checked independently.
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
recovery and project/revision listing do not use unbounded `fetchall()`.
The same closed recovery-table specification, bounded columns, per-table row
limits, aggregate row/byte limits, and per-value limits are evaluated inside
every SQLite write transaction immediately before `COMMIT`. The check observes
the transaction's final revision, activation-binding, idempotency, event,
upload, and cleanup state, so a successful mutation cannot create a database
that the same configured process cannot recover. A quota failure follows the
ordinary rollback path and is not reported as a post-commit or unknown outcome.

`project_revisions` is an immutable per-project ledger with a unique contiguous
generation and exact predecessor. Its Core-owned revision ID authenticates a
canonical manifest digest over project/task/workspace snapshots, registry
digest, generation, and predecessor. Recovery recomputes that identity, every
snapshot identity and revision ETag, the complete predecessor chain, and the
ProjectV1 active-head binding. Missing rows, gaps, cross-project predecessors,
noncanonical active transitions, and project/head divergence fail startup
closed. A successor activation timestamp is `max(wall_clock,
predecessor.updated_at + 1 microsecond)`, so it remains strictly increasing
under a fixed or regressing clock. New ledger rows also sign the canonical
idempotency request digest that activated that exact revision. A private audit
row binds the same revision and request to the exact successful idempotency
identity. It survives project deletion but is removed by the idempotency row's
retention cascade, preserving exact historical replay without unbounded audit
growth. Revision documents and activation bindings count against the same
irreversible startup row, per-value, and aggregate byte budgets as other
provider state. Legacy ledger migration first preserves the old rows in the
current schema; retained idempotency validation then backfills request digests
and activation bindings inside the startup recovery transaction. A final shared
pre-commit accounting pass includes those newly written rows and bytes. If the
backfill would cross a recovery limit, both the digest updates and binding
inserts roll back atomically.

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
the same validator. A ready project response must also match the exact immutable
ledger payload and the activation request digest stored for that revision. A
canonical response from another resource or a later generation is therefore
corruption, not an authoritative success.

Project creation signs Core-owned project and task snapshots. Scratch projects
also receive an immutable empty workspace snapshot. When and only when a
verified registry digest, ready model preparation, and workspace snapshot are
all present, the creation transaction also stores generation-zero active
`RevisionV1`, updates the complete ready `ProjectV1`, appends project and
revision activation events, and stores the idempotency response. Codex
subscription supplies ready model preparation; self-deployed model preparation
remains unresolved in this provider slice. Imported projects remain draft until
their declared archive is finalized. A ready project PATCH publishes exactly
one direct active successor in the same transaction and retains every older
revision. If a PATCH removes a readiness input, the prior active head is
retained while the changed project is draft; no successor is fabricated.
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
the project and upload, and, when registry/model readiness is complete, stores
generation zero or the direct successor revision. It appends the project event
and any revision activation event in that same transaction. Recovery verifies the
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
authoritative project ETag. A revision publication additionally emits the
frozen `revision.activated.v1` envelope with the immutable revision ETag and
project parent identity. Replay pruning never retains an activation without its
immediately preceding project update; if the configured window cannot hold the
pair, it retains neither half. Deleting a project truncates the replay prefix
through that project's final retained event before its revision ledger is
removed, so no orphan activation remains. Frame sequence is monotonic and the
opaque ID is authenticated by the store key. A retained `Last-Event-ID` resumes after that
sequence. A valid cursor older than the window returns 410 so the sidecar
reloads snapshots; malformed, tampered, or future cursors return 400.

The stream polls durable state, emits retained records in order, and persists a
heartbeat after 15 seconds without another frame. Restarting Core preserves the
same cursor key and replay order.
Startup and replay require canonical frame bytes and authenticate each stored
frame ID against its exact row sequence. Startup additionally requires a
contiguous retained suffix and checks every ready project-update/activation
pair against the exact immutable ledger revision, including adjacency,
generation order, snapshots, registry digest, timestamps, resource identity,
parent identity, and ETags. Missing, reordered, duplicated, or shape-valid but
substituted rows fail closed. A shape-valid but unsigned persisted cursor is
store corruption.

Typed conflict metadata is code-specific. Snapshot and registry races are
retryable after an authoritative reload; malformed archive or config requests
are non-retryable and `openevo_can_reconfigure`; storage quota exhaustion
requires user action; detected durable-state corruption is non-retryable.

## Verification

Provider coverage is in
`tests/backend/test_core_control_v1_provider.py`. The frozen contract tests in
`tests/backend/test_contract_v1.py` continue to prove exact OpenAPI and event
schema bytes and digests. The release owner implements generation-bound run
admission, managed service preparation, durable execution, artifact-list
projection, and direct cross-session successor activation. Standalone artifact
content/diff, environment repair, service mutation, and diagnostics remain fail
closed.

The provider accepts either an internal `CoreRunControl` dependency or a
mutually exclusive factory that receives the provider's exact durable store. When it
is absent, every frozen run route remains the same typed unavailable response.
When present, the provider delegates the complete run route family and status
counts to that owner and installs the private generation-authenticated admission
endpoint; it does not compile experiments or inspect method logic itself. This
dependency boundary keeps the public OpenAPI and Desktop client unchanged while
run ownership evolves. The release launcher always uses the factory to construct
`CoreScienceRunOwner`; see `core-science-run-owner.md` for its persistence and
cross-session contract.
An anti-drift test derives every `/v1/runs*` operation ID from the frozen route
table and requires exact equality with `RUN_OPERATION_IDS`; the run artifact
route is therefore owned as `listCoreRunArtifactsV1`, its canonical operation
ID. A retryable `CoreRunControlError` remains a transient observation and is not
written to the provider failed-idempotency table. For state upgraded from an
older provider, replay compares a retained run error's request digest in
constant time before parsing or cleanup. A conflicting request therefore
returns `idempotency_key_reused` without deleting the original row. An exact
replay validates the retained error inside an immediate transaction and
conditionally deletes the exact row when it is retryable before invoking the
owner. A failed cleanup transaction or unknown post-commit lifecycle
verification never invokes the owner; a committed cleanup allows the next
retry to proceed.

The provider applies a thread-safe, 256-entry single-flight table only to keyed
create, cancel, retry, and delete run mutations. Concurrent requests with the
same operation, scope, key, and digest share one owner call and receive the same
success or error payload. A concurrent request that reuses the identity with a
different digest conflicts before owner invocation. Successful results remain
in a bounded process-local LRU replay cache. A failed flight leaves the replay
map immediately, but while admitted waiters are still resolving it remains in a
non-replay drain set that shares the same 256-entry capacity bound. An exact
digest may start a fresh owner call instead of replaying that error; a different
digest still conflicts until the concurrent failed flight drains. Retryable
owner errors can therefore be retried, while non-retryable errors continue to
replay from durable provider storage. Eviction, process crash, and restart fall
back to the run owner's durable idempotency authority. When replay and drain
entries exhaust capacity, a new identity fails with a typed retryable capacity
error.

Shutdown stops admission to both sets, freezes their combined drain authority,
and waits up to 30 seconds for every admitted callback and coalesced waiter to
resolve before it closes or clears the run owner. The wait never holds the
single-flight lock. A timeout leaves the owner, store, and same drain set intact;
a later idempotent `close()` continues that drain instead of admitting new work
or losing authority. Only a completed drain permits owner and store teardown, so
neither a delayed leader nor an awakened failure waiter can escape authority,
fall through to a synthetic unavailable response, or permit premature teardown.
Non-run operations and run reads are unchanged.

The run owner's science execution projection preserves the exact project
`capture_mode` in both experiment agent settings and the evolution execution
profile. Subscription execution accepts only transcript capture. Self-deployed
token-level execution uses proxy auth and a token-level profile, while a
self-deployed transcript project remains transcript; neither mode is silently
rewritten. Both execution modes project the Core-owned `managed_science`
profile, exact managed image, host-user container binding, managed `HOME`/`PATH`,
and private runtime-directory preparation into `ExperimentConfig`; proxy mode
also binds the managed Codex home. This keeps the project-to-run bridge inside
the same validated runtime contract as the standalone science compiler.
