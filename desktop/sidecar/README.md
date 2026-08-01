# Desktop sidecar

The sidecar owns Desktop-local security boundaries that must not be exposed to
the React renderer. Local HTTP routes and the native host are separate adapters;
they are not implemented in every private sidecar service module.

## OpenSSH host catalog

Local API v2 exposes a path-free catalog of literal aliases from the user's
default OpenSSH configuration. `ssh_config_catalog.py` performs only bounded
lexical reads of `Host` and statically resolvable `Include` directives. It does
not invoke `ssh -G`, `Match exec`, `ProxyCommand`, a shell, or any other external
program. Wildcard, negated, conditional, malformed, unreadable, cyclic, and
over-budget entries produce closed warning codes; a bounded literal alias can
still be entered manually.

The loader has immutable limits for file count, aggregate and per-file bytes,
include depth and patterns, glob matches, line bytes, and unique aliases. Its
result contains aliases, source kind, and aggregate warnings only—never config
text or source paths. The v2 catalog provider assigns a semantic generation,
keeps that generation stable when a rescan is unchanged, and binds every rescan
to both the expected generation and an idempotency key. This catalog is a UI
hint: the selected exact alias is later passed to system OpenSSH, which remains
the connection and configuration authority.

## System OpenSSH remote-home authority

Once the owned OpenSSH master authenticates, `SystemOpenSshSession` runs one
fixed private account probe through that exact master. The probe binds the
effective `id` username/UID to a single NSS `getent passwd <uid>` record and
requires a safe normalized, physically identical, owner-matching, writable home.
Its bounded stdout/stderr bypass lifecycle observation. The parsed
`RemoteHomeAuthority` is sealed to the profile ID and connection generation and
exists only in process; no renderer DTO, Desktop Local API model, profile row,
event, diagnostic, or log contains the home or its derived roots.

`SystemOpenSshRemoteLifecycleV2` derives the private workspace root as
`<home>/.openevo/workspaces` and gives the same authority to the follower and
deployment transport. The follower guards each rich remote shell command by
rechecking the effective account, NSS home, physical path, owner, and
writability. `SshRemoteExecutorTransport` requires an exact profile ID,
generation, user, and explicit workspace-root match and derives Daemon staging
only as `<home>/.openevo/daemon-bundles`; only the legacy explicit maintainer
transport retains the conventional username helper. The Daemon stage performs
its own equivalent admission, rejects symlink/drift/replacement, and rechecks
the owner-private root identity around transfer and publication. The Core tunnel
is deliberately different: it stays a raw, non-shell `ssh -W` channel because
it executes no home-derived command.

An unsupported or drifting remote account fails closed as
`ssh_remote_account_unavailable`. The release provider replaces all private
details with the fixed writable-home summary and `administrator_action` before
operation, profile, event, or persistence projection. SSH and Daemon process
logs pass through the absolute-host-path sanitizer before storage.

## V2 provider state and Preview import

The Desktop v2 local-state composition owns a separate owner-private
`provider-v2/` namespace beneath the existing Desktop state root. Its SQLite
schema and migration history have frozen startup fingerprints and use
rollback-journal transactions. Every operation revalidates the held root, lock,
database inode, size budgets, and the canonical closed documents it reads. A
committed mutation and its idempotency response are one transaction, so a retry
after process loss returns the original profile or draft without duplicating it.

Process-restart reconciliation never treats a stale SSH control socket as live.
It invalidates each process-owned connected profile to a new disconnected
generation. When a queued or running project-create/project-activate operation
depends on that profile, the same SQLite transaction reserves one deterministic
`profile_connect` prerequisite and advances the profile to `connecting`. The
single lifecycle executor defers the original project operation, reconnects
through system OpenSSH, and then resumes the exact persisted project request and
action identity; it does not issue another project mutation. A restart while the
prerequisite is pending resumes that same prerequisite, while an explicitly
disconnected profile is not reconnected automatically.

The retained v1 `provider.sqlite3` is opened only through a shared owner lock and
SQLite read-only/query-only mode after exact root, file, schema, and inode
validation. Import reads lengths before bounded cells and applies one aggregate
budget. Valid explicit profiles become non-connectable `legacy_explicit`
records containing only a display name and opaque digests. Host, user, port,
authentication, proxy, cached revision, and remote authority are never copied.
Corrupt or oversized profile rows become generic quarantined records; invalid
project rows are skipped. Both cases produce bounded typed diagnostics, and an
unsafe or unavailable legacy store does not prevent unrelated fresh v2 state
from starting.

Legacy draft documents stay process-local migration input. Copying one requires
the user-selected system-OpenSSH profile and a complete `ScienceProjectConfigV2`
that passes strict validation. The v2 store persists only that canonical config
and its digest, never a cached v1 remote state or generic revision.

## V2 Core bridge authority

`core_client_v2.py`, `core_bridge_v2.py`, and `core_bridge_adapters_v2.py`
implement the strict project-bound Core Control API v2 boundary. Before Core is
compatible, the adapter may stage and start the sealed Daemon bundle through the
selected system-OpenSSH profile. After compatibility negotiation, every project,
workspace, task, attempt, service, artifact, diagnostic, and event operation uses
only that active project's verified loopback tunnel. The HTTP transport accepts
only its fixed private loopback origin and cannot fall back to a launcher URL,
shared backend URL, v1 route, or direct SSH business command.

The client verifies the exact release, OpenAPI, event-schema, registry, runtime,
and Daemon identities before publishing an authority generation. JSON and SSE
inputs are duplicate-key rejecting and bounded before model validation; cache
updates are copy-on-write, results are generation-sealed, and shutdown has a
global bounded close capacity. Remote failures cross the bridge only as closed
Local API errors without URLs, bearer values, SSH commands, host paths, or
upstream validation objects.

`core_bridge_store_v2.py` owns a separate private SQLite namespace for exact
Desktop profile/project-to-Core authority mappings and mutation replay records.
Mappings preserve distinct Project Head, Evolution Revision, Runtime Context,
execution snapshot, registry, runtime, Daemon, and event-cursor identities; no
context-dependent generic revision exists. Mutations are recorded before send,
move through explicit unknown-outcome recovery, and become applied only after an
exact typed authority result. `event_broker_v2.py` provides bounded canonical
local replay and subscriber queues. Release-provider routing and renderer-facing
Local API v2 composition are wired separately.

## Release execution modes

The exact sidecar release composition publishes the required
`DesktopStateV1.execution_mode_capabilities` contract before any project or
remote connection exists. The list is closed, versioned, bounded, ordered, and
must contain each Local API v1 execution mode exactly once. It is release
support, not remote-host readiness: model preparation, GPU/runtime checks, and
credentials continue to come from Core/project diagnostics after connection.

The current composition reports Subscription as `supported` and Self-deployed
as `unavailable` with the stable `self_deployed_release_unavailable` reason.
Create, update, activation, and run paths enforce the same object before any
project persistence, activation reservation, SSH, or Core side effect. Existing
Self-deployed projects remain readable and can be changed to Subscription.
Changing the composition entry to `supported` after the self-deployed serving
implementation ships enables the existing renderer without a React mode table.

## Workspace imports

`WorkspaceImportStore` is the private persistence and verification layer for a
native-folder snapshot. Its ingest boundary accepts only an already-open,
seekable regular-file descriptor or a binary stream backed by such a descriptor.
It does not accept a host path, URI, archive bytes object, or renderer payload.
The Tauri host sends the canonical path plus its selected device/inode only to
the authenticated private loopback route. `native_workspace.py` reopens every
absolute component with no-follow semantics, requires the final directory to
match that exact identity, creates the canonical archive in an unlinked private
temporary file, and passes only that open regular-file stream to this store.
Each directory enumeration consumes the single global entry budget as soon as
names are yielded, including siblings not yet visited recursively. The scanner
collects at most the remaining budget plus one before rejecting or sorting, so
an oversized directory cannot force an unbounded list, sort, or sequence of
directory-entry system calls.

Every non-empty file must independently pass both allocation and extent checks.
`st_blocks` is interpreted in its POSIX 512-byte units and must cover the complete
logical size; `SEEK_DATA`/`SEEK_HOLE` must then prove one data extent from zero to
EOF. A filesystem's permitted minimal extent implementation can therefore not
hide a low-allocation sparse file. Platforms without either proof fail closed.
Ordinary fully allocated APFS files are accepted even when extent calls return
the minimal `0, size` map; compressed, dataless, or other files whose allocation
metadata is below logical size are rejected with an explicit sparse/compressed
unsupported error because the bridge cannot prove a complete snapshot.

Ingest validates uncompressed `openevo_deterministic_tar_v1` byte for byte while
streaming it into an owner-only store. Validation covers the frozen POSIX ustar
header encoding, NFC UTF-8 safe relative paths, canonical path splitting and
ordering, parent entries, allowed file types and modes, per-file and aggregate
budgets, body padding, checksum, and exact terminator. Neither the archive nor an
archive file body is buffered in full.

Initialization opens every absolute ancestor component with no-follow directory
semantics and retains stable parent/root descriptors. First initialization is
serialized by a root-name-scoped private file in the parent directory; every
process locks and revalidates that file before reading or advancing bootstrap
state. A closed, authenticated bootstrap record temporarily holds the generated
authentication key and store token. The separate mode-`0600` authentication key
then signs the canonical parent/root binding marker and import metadata. The
marker binds the store token to the parent and root inode; the same token is
stored as a durable root xattr.

The bootstrap record, authentication key, pending marker, and final marker are
each written to a random private temporary inode, fsynced, identity-checked, and
atomically published with a no-replace rename before the parent directory is
fsynced. A failed pre-publish attempt cleans up only the inode identity recorded
at creation. An interrupted process may leave an unpublished random temporary;
later initialization neither trusts nor removes that unknown pathname. The
authenticated bootstrap record lets a fresh store resume after key publication,
while a key without bootstrap, authenticated pending, or final state fails
closed. Fresh creation publishes the pending marker, fsyncs the new root parent
entry and root token, publishes the final marker, and only then removes pending
and bootstrap records. The existing root lock continues to serialize recovery
and import operations after binding completes.

Every operation reopens the ancestor chain and verifies the key, marker, parent,
root pathname, held root FD, and xattr. During one process lifetime, held
descriptors detect replacement at operation boundaries. After restart, missing
keys and keys that no longer authenticate existing state fail closed;
ancestor/root/marker/xattr replacement is detected while the separate key
remains confidential and unmodified and the owner-only state directory has
retained its integrity.

Extended attributes are accessed only through verified file descriptors. Linux
uses Python's descriptor xattr API; macOS binds libc `fgetxattr`, `fsetxattr`,
and `fremovexattr` directly, with no pathname or `/dev/fd` fallback. Reads and
writes are bounded, and native errno values remain available to store recovery
logic.

The store root and import directories are mode `0700`; archive and closed
metadata files are mode `0600` and link-count one. The store normally creates a
random opaque import ID. The native route instead supplies an action-derived,
opaque ID so exact retries converge without using a path or content as identity.
Archive plus metadata publish
as one fsynced directory through an atomic no-replace rename. Ingest records the
temporary directory's device/inode identity immediately after creation. Every
pre-publish rollback supplies that identity to quarantine cleanup; a missing or
same-name replacement binding is preserved for bounded startup reconciliation.
Metadata binds the exact `WorkspaceImportRefV1`, private storage identity, random
archive generation token, and a store-internal `WorkspaceImportOwnership`
containing the Desktop `project_id`, workspace-sync `operation_id`, and request
idempotency key. Native operation/idempotency authority is domain-separated over
the project/import owner, opaque import ID, and archive digest. Two projects may
therefore retain byte-identical snapshots, including empty archives, while an
exact action replay for the same owner converges and an owner/content conflict
fails closed. The
native/provider adapter must pass that exact ownership to `ingest`, `resolve`,
and `release`; these fields are not added to the frozen HTTP DTO. Retrying ingest
with the same ownership and content returns the already-published reference,
including after a post-publish crash. Reusing an operation/idempotency key for
different content or another owner fails closed. External references must use
the exact store-issued `workspace-import-` plus 48 lowercase hexadecimal grammar
before any filesystem operation.

The store retains at most 10,000 imports and 24 GiB of archive bytes by default.
Those aggregate limits are checked under the cross-process root lock before each
ingest. Their reconciliation reservation covers root and child enumeration,
maximum metadata bytes, two full archive hash passes, deterministic-corruption
cleanup, and one interrupted publish temporary directory. Startup reconciliation
therefore remains bounded without increasing its 300,000-node/64-GiB budgets.
Release startup defers destructive reconciliation until the provider has obtained
the exact durable project-reference snapshot. It validates every referenced import
before removing any orphan; a missing or corrupt reference preserves all observed
state and fails startup closed. Only after that phase succeeds does it remove
recognized temporary/unknown entries and deterministically proven unreferenced
corruption without following symlinks. Filesystem and xattr `OSError` failures are
treated as infrastructure failures: reconciliation keeps the observed entry and
fails closed for a later retry.

Native picker ingest first creates a pending lease. The lease token is a
domain-separated HMAC over the exact import reference and ownership; only its
one-way marker is persisted as an authenticated archive xattr. The hidden native
response carries the token only to Rust, which keeps at most 64 pending handoffs
keyed by renderer action ID and returns only `ProjectSourceV1` to React. The
renderer can request native `cancel`, `adopt`, or `discard` by action ID but never
receives the cancellation secret, lease token, or hidden route. Rust releases the
action claim immediately on cancel, sends the secret-bound request, and limits a
cancelled import response wait to three seconds. Both native and sidecar retain
64-entry cancel-before-start tombstones so close/invalidation cannot race a queued
picker command into a new ingest. Python checks cancellation while traversing,
copying, hashing, validating, and waiting for either import lock. Publication is
the linearization point: cancellation before rename removes only the bound
temporary inode; cancellation after rename returns the recoverable pending lease
to Rust, which records it before requesting guarded discard. Create/patch commits
adopt only after project state is durable. Close, reselect, reset, stale picker
completion, and failed save paths discard. Discard first takes the provider
reference guard, rereads all durable project references, and only then takes the
import lock, so it cannot remove an import concurrently committed by another
request. Referenced pending imports are adopted during startup recovery;
unreferenced leases are removed. Total retained limits remain 10,000 imports/24
GiB, while pending state is separately capped at 64 imports and 16 GiB by default.

The only renderer/public ingest result remains the existing closed contract model:

```text
WorkspaceImportRefV1 {
  import_id,
  content_sha256,
  byte_size,
  entry_count,
  extracted_byte_size
}
```

`resolve(import_ref, ownership=...)` reopens metadata and archive with no-follow
semantics and rehashes the stored archive. While holding the root lock it copies
the bytes to a private temporary inode, fsyncs and verifies the complete snapshot,
then rechecks the source identity, size, and digest before commit. The temporary
pathname is inode-bound, quarantined, and unlinked before a read-only descriptor
is yielded. The consumer therefore never receives the mutable stored archive FD
or a host path; its stream `name` remains an integer descriptor.
`release(import_ref, ownership=...)` and `delete(import_ref, ownership=...)`
remove only an exact verified reference owned by that project operation. Cleanup
first moves the observed inode to a random quarantine name, verifies the binding,
and only then removes flat no-follow contents. Tamper, replacement, unsafe root
state, unavailable atomic rename/xattr support, and reconciliation budget
exhaustion all fail closed.

This owner-only filesystem store is not an isolation boundary against an
arbitrary process running as the same UID. Such a writer can read or replace the
authentication key and can mutate an already-open regular-file inode. The
restart and replacement guarantees above therefore require that the key has not
leaked and that the private state directory has not been compromised. Stronger
same-UID isolation requires a platform credential boundary outside this module.

The private import, cancel, and discard routes are excluded from OpenAPI and
require a process-owned native handoff token that is distinct from the renderer's
Desktop session token. Its bounded import request is the only path-bearing
message; its private success envelope contains `ProjectSourceV1` plus the
native-only lease and never echoes that path. Rust strips the lease before
returning to React. Native import ownership is reproducible from project/import
owner, opaque import identity, and archive digest. A new project created from a
native import receives a deterministic project ID derived from the opaque import
ID; an existing project supplies its own ID privately.
The release provider verifies the exact import and ownership before persisting a
native project source. Project reference mutations and import cleanup use one
fixed lock order: provider reference guard, then import-store root lock. A source
patch compares `import_ref` identity rather than display metadata. Post-commit
cleanup reacquires the guard, rereads the complete durable reference snapshot,
and releases the old exact import only if its ID is still unreferenced. Startup
uses the same coordinated snapshot and retains only exact reference/ownership
matches while removing abandoned picker imports. Core upload consumption remains
the next integration step.

## Frozen Local API v1 implementation (0.1.8 historical)

The remainder of this document describes the frozen v0.1.8 Local API v1 and its
read-only migration inputs. It is not the packaged v0.1.10 composition. The
active release path is the v2 provider, bridge, lifecycle, and system-OpenSSH
authority documented above and in `docs/architecture/desktop-core-contract-v2.md`.

The sidecar also owns the renderer-facing Desktop Local API and the process-owned
connection to remote OpenEvo Core. The canonical public contract is defined once
in `contracts/v1/app.py`; release implementations must use its provider injection
point instead of registering another route table.

### Historical implementation

`release_app.create_release_desktop_local_api_app()` creates the real Local API v1
application. It owns one `DesktopProviderStore` and one `WorkspaceImportStore`
for the process lifetime and
requires the native host to supply a Desktop session token, native instance ID,
readiness key, source commit, and private state root.
The launcher assigns that root to the `state-v2` Desktop storage namespace. This
name is independent of the Local API v1 contract. On macOS it is rooted at
`~/Library/Application Support/org.openevo.desktop`; v0.1.7 preserves and does
not read or alter the old Preview `~/.openevo/desktop/local-api-v1` state.
The release application wraps the complete FastAPI/Starlette stack with CORS,
including the server-error boundary, so bounded error responses remain readable
by the packaged renderer. Only
the exact packaged Tauri origins (`tauri://localhost` and
`http://tauri.localhost`), used methods, the standard CORS-safelisted headers,
the renderer contract headers, and the browser-generated `Cache-Control` and
`Pragma` headers required by the renderer's `cache: "no-store"` requests are
accepted;
wildcards, credentials, lookalike origins, unknown methods, and unknown headers
are rejected. CORS preflight is handled before Desktop session authentication,
while ordinary `/desktop/v1/*` requests still require the exact ephemeral
session header.
Embedded callers get an ASGI shutdown close hook by default. The packaged
launcher explicitly sets `close_on_shutdown=False` and becomes the single
provider shutdown owner so listener/provider cleanup failures can be converted
to a fixed `shutdown_failed` process diagnostic instead of being absorbed by
Uvicorn lifespan state.
For the packaged process only, the launcher temporarily installs a signal
replay handler around Uvicorn and explicit cleanup. Uvicorn can therefore turn
Tauri's `SIGTERM` into graceful server exit without the replayed signal killing
the process before provider cleanup completes.
If app or Core-runtime construction already has a primary failure, all resources
created so far are still given a best-effort close, but cleanup exceptions do
not replace that primary exception.
Runtime shutdown independently attempts relay stop/join, bridge, broker, and
bridge-store cleanup. If several fail, callers receive the first failure only
after every owned resource has been attempted.
The stop/close sequence is linearized under the runtime close lock, so a second
thread cannot report close completion while bridge shutdown or relay join is
still active. Provider shutdown applies the same first-failure aggregation
across executor, runtime, lifecycle, provider store, and workspace store.

Before the Local API reaches readiness, packaged startup failures use the closed
`OPENEVO_STARTUP_V1` stderr contract. Only fixed stage/code pairs and an
optional numeric errno are recognized. Paths, argv, environment values,
credentials, URLs, exception messages, tracebacks, and arbitrary process output
are never surfaced. Release smoke tooling uses a bounded OS pipe, scans at most
32 KiB, and emits at most eight allowlisted records, so this contract does not
create telemetry, an unbounded process log, or a secret-bearing diagnostics
channel. The packaged Python launcher records only the last fixed release
composition phase, covering embedded Core assets, provider/credential stores,
SSH lifecycle, workspace storage, Core bridge composition, Local API routing,
native Local API routing, static application mounting, native frame handoff,
listener setup, and server startup. A failure emits that phase's closed
`*_failed` code; the original exception and its cause remain private.

The current provider implements:

- public `GET /version` with `provider_kind=desktop_sidecar` and the canonical
  OpenAPI digest;
- challenge-bound `GET /health` using HMAC-SHA256 over
  `protocol NUL instance_id NUL challenge`;
- constant-time Desktop session authentication for every `/desktop/v1/*` route;
- `GET /desktop/v1/state` with the process-owned SSH/Core lifecycle state;
- profile and project list/create/get/patch/delete through
  `DesktopProviderStore`, including durable idempotency, signed cursors, ETags,
  and restart recovery;
- profile connect/disconnect plus explicit SSH host-key review and acceptance.
  The sidecar probes without trusting, repeats the probe before confirmation,
  gives credential resolution, trust-store load/probe/confirmation, transport
  construction, and the trusted SSH check one shared 12-second deadline,
  stores only the confirmed fingerprint in Local API resources, and owns the
  trusted known-host file under its private state root. Unconfirmed candidates
  remain only in the process-owned review state and restart recovery removes any
  candidate persisted by an older interrupted implementation;
- private identity-bound native folder import, project-source verification, and
  committed/startup lifecycle cleanup;
  this route is deliberately absent from the public Local API OpenAPI document.

The dedicated Core bridge store commits a generation-zero `pending` identity
before publishing its root-local marker and parent anchor, then marks the row
`bound`. Restart completes only that exact empty, inode-bound pending bootstrap.
A torn first-slot write is replaceable only while that row remains `pending` and
the never-published backup slot is still zero; malformed, empty, or missing
markers in a `bound` store fail closed. Unknown old state is never adopted.
Durable unknown workspace finalize authority is replayed with its original
request, ETags, and key before the bridge applies any newer Local project or
workspace patch.

Connection mutations atomically reserve idempotency capacity, two fixed terminal
response slots for the operation and idempotency documents, profile action
ownership, and a running operation before external SSH work. One process-wide
action lock serializes that full reservation, SSH invocation, and finalization
cycle across every profile, route, and idempotency key. Replacing profile A with
B therefore closes and durably disconnects A before invoking B. Disconnect is
non-displacing: its reservation does not publish `connecting` or alter another
profile, and the sidecar rejects a profile that does not own the process
lifecycle before calling the transport. Success, error, and recovery
cancellation finalize within the reserved slots without another capacity or
request-ETag check. If completion reports an error before commit, the running
reservation retains its terminal capacity until failure is durable. If commit
succeeded before returning an error, the frozen success remains authoritative
and its transport stays open even if concurrent CRUD consumed the released
capacity. Failure finalization resolves the same return ambiguity with a
read-only observation bound to the exact idempotency envelope and reserved
operation. It retries only a proven `running` state. A durable failed operation
authorizes cleanup only while the profile remains durably disconnected and the
process transport still has that profile as owner; exact failed replay repeats
this check so interrupted cleanup converges without closing another owner's
transport. Failed operations retain their bounded `ApiErrorV1`, so exact replays
return the same error and do not repeat remote work. Once any operation is
terminal, its body and ETag are immutable; a late complete/fail call only returns
that terminal and may close the transport owned by its own stale result. Restart
only cancels truly nonterminal reservations, updating their operation and
idempotency documents in the same recovery transaction.
Profile deletion checks for queued, running, or cancelling profile operations in
the same write transaction as the delete, so even a non-displacing disconnect on
an already-disconnected profile retains its resource authority through terminal
publication. Terminal historical operations do not prevent later deletion.

Long-running project work uses a separate durable reservation lifecycle. The
store binds the route, project, request body, `If-Match`, and idempotency key in
one transaction, publishes a `queued` `LocalOperationV1`, and reserves bounded
space for both the operation and replay documents before an executor is allowed
to submit work. Starting and terminal publication update the operation and its
idempotency replay atomically. While a project reservation is queued, running,
or cancelling, project patch/delete and a second project action fail closed so
the immutable project snapshot consumed by Core cannot change underneath the
worker. Because activation also demotes the previous active project and changes
its ETag, its reservation covers the target project and that implicit
cross-project write. A queued, running, or cancelling operation on the current
active project excludes another project's activation; once the activation is
reserved, it symmetrically excludes new work on the active project and any
competing activation. The store checks the same exclusion again in the atomic
activation completion transaction, excluding only that activation's own
reservation, so an authority conflict cannot be hidden by an earlier start-time
ETag check. Project intent patch uses that same global activation-authority
check: while another project's queued/running/cancelling activation owns the
replacement of the current active project, patch cannot demote that active
project or clear its revision/remote projection. Activation success requires a
complete ready `RemoteProjectStateV1` and atomically records the active project,
its matching Core revision, the canonical remote projection, the terminal
operation, and its idempotency replay. Other project operations cannot publish
remote state or invent a project result. The remote projection is a durable
observation carrying the sidecar observation time, not proof that the SSH tunnel
or Core remains live. Startup
therefore preserves it as history while resetting local active/current-revision
runtime authority under the existing recovery rule. Ordinary demote/archive
transitions also preserve that observation, while any project intent patch
clears both the remote projection and revision because they describe the
previous intent. This closed projection has a separate 256 KiB per-row limit and
16 MiB aggregate recovery limit. Schema v5 keeps one closed
`provider_storage_usage` authority row containing the complete provider recovery
row/byte totals, remote payload count/bytes, four remote-content accumulators,
fixed live-action reservation counts, exact idempotency/cursor row counts, a
generation, and a domain-separated HMAC sealed with the separate owner-only
cursor key. Every mutable provider table has canonical insert/update/delete
triggers that update this authority, invalidate its seal, and require exactly one
affected singleton row. The write transaction reseals before commit, so rollback
restores resource, operation, idempotency, cursor, and usage state together.
Normal capacity checks are primary-key reads of the singleton; they do not run
provider-table `count(*)` or aggregate scans.
Recovery accounting charges the singleton a fixed conservative 512-byte
reservation instead of recursively measuring its changing decimal counters.

Normal idempotent create, profile-action, and project-action writes first reclaim
at most 128 expired cleanup-eligible replay rows through the v5
`(cleanup_eligible, expires_at_epoch)` index. The exact idempotency key for the
current request is then read by primary key and may cause one additional expired,
cleanup-eligible row to be deleted. An idempotency write therefore deletes at
most 129 rows: a 128-row indexed sweep plus one exact-key point cleanup. A
nonterminal operation replay is not cleanup eligible, so it remains available
even after its original retention time; terminal publication atomically makes
the replay eligible. Pagination cursor writes remove strictly at most 128 expired
rows through the expiry index. The authenticated counters are then used for exact
capacity decisions. This bounds foreground cleanup work independently of table
size while preserving live-action replay and hard capacity limits; additional
expired rows are reclaimed by later writes.

At open, after authenticating the singleton and before any startup mutation, the
store compares both configured record limits with the persisted exact counts. A
limit lower than its persisted count raises
`ProviderCapacityConfigurationError`; open does not try cleanup and commits no
startup state. Repeating the same incompatible open is read-only and fails the
same way. Reopening with limits at least as large as the reported persisted usage
is the recovery path, after which successful writes can commit bounded cleanup
batches. The same check runs before a v4 -> v5 migration is sealed or committed.

The singleton cannot be inserted or deleted after v5 initialization, and the
migration ledger is immutable. `DELETE` plus reinsert and `INSERT OR REPLACE`
therefore fail even with `recursive_triggers=0`. Each remote payload also stores
a per-project HMAC-derived token over its project ID and exact canonical bytes.
Triggers maintain the authenticated modular token accumulators, while project
get/list recomputes each selected row token before JSON parsing. A process-local
last-committed generation/seal snapshot rejects online replay of an older signed
authority row.

Startup validates the seal, applies the existing 100,000-row and recovery-byte
budgets, and then performs one bounded reconciliation of real table totals,
remote lengths/tokens, and live reservations before decoding remote payloads.
The v4 -> v5 migration does the same and publishes project tokens, the authority,
triggers, migration row, and schema version in one SQLite transaction. After the
singleton and v5 migration row exist, but before the authority is sealed or
`user_version` changes, migration validates the final authority against the full
write row/byte/reservation budgets and the configured record limits. Failure
rolls the transaction back to the reopenable v4 layout. Oversized,
configuration-incompatible, noncanonical, trigger-tampered, partially replayed,
or equal-length rewritten state fails closed.

The v5 -> v6 migration adds the explicit evolution-configuration state to
projects and `ProjectV1` idempotency replays. The v6 -> v7 migration replaces
only the historical subscription value `codex_model="gpt-5"` with `gpt-5.5`;
other invalid model references are not inferred or rewritten. Both migrations
probe row counts and BLOB lengths before exact-length guarded reads, validate
the complete closed models, rebuild and reseal storage usage, and publish DDL,
ledger row, and `user_version` in the same rollback transaction.

The authority assumes the owner-only signing key remains confidential and is not
an external monotonic anchor. An offline attacker who restores a complete earlier
database snapshot, or who reads the key and coherently rewrites every authenticated
row, is outside this SQLite module's detection boundary. Detecting complete offline
rollback requires a platform-protected monotonic value outside both the database
and key file. Budget-changing edits, remote-content edits, and process-lifetime
authority replay are detected; same-length model-valid edits to other resource
fields remain inside the owner-only state-directory threat boundary.

Project intent remains editable after activation so the UI can activate once to
obtain remote capabilities, edit evolution configuration, and activate again.
When no project operation is queued, running, or cancelling, patch commits the
new `ProjectCreateV1` document, demotes local `active` or `blocked` state to
`draft`, clears revision/remote state, and advances the project ETag exactly once
in one transaction. A busy project still rejects patch. The saved draft must be
reactivated before any prior remote projection can be used.

A typed failure keeps the project draft and is replayable without repeating
remote work. Startup cancels every nonterminal
reservation exactly once and updates the replay in the same recovery
transaction, releasing all direct and implicit activation exclusions before
new work is accepted. The release provider places activation reservations on one
serialized executor with a hard 16-item admission bound. The HTTP route returns
the durable queued operation without waiting for SSH or Core. Once executor
admission succeeds, a start gate first publishes `bootstrapping` with
`active_tunnel=false` and the accepted operation ID; the worker cannot execute
before that non-readable state is observable. Queue rejection fails only the new
reservation and preserves the prior session authority. The worker then publishes
`running`, calls the project-bound bridge outside SQLite transactions,
validates the returned project/revision/registry identity, and commits the
complete remote projection and terminal operation atomically. It then
acknowledges that exact activation authority against the post-commit Local ETag
before reporting Core `online`. Bridge failures retain their typed error with
the Local operation request ID; unexpected local failures are sanitized. A
published Core session is retired whenever Local completion or acknowledgement
fails, including when the terminal operation is already durable, so a stale
tunnel cannot retain Local authority.
Profile lifecycle actions, activation start gates, and project retirement share
one process-local session generation. Admission is ordered under the
project-session transition lock, while external work remains outside the state
lock. Every terminal path rechecks the generation. A stale profile success or
failure finalizes its durable operation without changing the replacement Core
state or disconnecting its transport; a stale activation cannot publish its
Local project projection.

`pending_operation_ids()` exposes only queued, running, and cancelling operation
IDs in stable identity order, with the same recovery-row upper bound, for
assembling `DesktopStateV1` without an unbounded query.

The production release resolver supports only `ssh_agent`. Native password and
private-key authentication are reserved contract values and are rejected by
create, patch, and connect; startup clears historical credential-slot status.
The packaged sidecar has no credential vault or native credential handoff route.
Authenticated proxy and Hugging Face token slots are unavailable. Profile proxy
URLs and `no_proxy` are projected into the remote profile, but user information
in proxy URLs is rejected by the contract.

When release composition supplies an active `DesktopCoreBridgeV1`, the provider
forwards capability and validation reads plus Core-owned runs, timelines, logs,
artifacts, services, diagnostics, maintenance, and operations through that
bridge. Run admission first matches the saved Local project ETag; capability and
validation envelopes bind the returned Core authority to the current Local
project identity and ETag. Typed Core failures are preserved without exposing a
Core URL or bearer. Without an injected bridge these routes remain fail-closed.
Ordinary bridge calls and the Core SSE relay report session loss through one
provider callback carrying the exact project/profile/ETag and process-local
session generation. Only the closed local allowlist (`core_client_closed`,
`desktop_core_bridge_closed`, `active_project_session_unavailable`, and a
generation-matched `active_project_session_superseded`) changes `core` to
`offline`, clears `active_tunnel`, and publishes state invalidation. A stale
generation cannot downgrade its replacement. Remote business errors and
`core_connection_failed` do not change connection authority because the latter
also represents request deadline expiry and therefore does not prove tunnel
loss.

When release composition also supplies `DesktopEventBrokerV1`, the provider
serves its bounded SSE subscription directly and maps expired cursors to the
frozen 410 reset response. Project activation now uses the durable bounded
executor and project-bound bridge described above. Packaged startup now creates
the production SSH adapter, bridge store, bridge, event broker, and Core event
relay from the exact embedded Core wheel/framework-lock pair. It advertises the
complete frozen release feature set: `remote_profiles`, `project_validation`,
`operation_events`, `run_observability`, `artifact_inspection`,
`service_control`, `diagnostics`, and `maintenance`. A composition failure aborts startup; it
never falls back to a local method table, direct backend URL, fixture data, or a
synthetic ready/success state.

Every direct Core request first loads the one durable active Local
`ProjectV1` plus its exact process-local session binding while holding the
provider's project-session transition lock, and it keeps that lock through
bridge delivery. Editing or retiring that project must
therefore wait for the in-flight result, after which the bridge generation is
retired before another project can use the provider. The event relay separately
passes the same complete active project and captured session generation to the
bridge SSE boundary. It ignores
Core heartbeats and translates every other validated Core frame into a Desktop
state invalidation; it does not copy, reinterpret, or persist Core event
payloads. The relay advances the Core resume cursor only after that invalidation
has been accepted by the Desktop broker and only across a contiguous Core event
sequence. Publication failure reconnects from the prior cursor; duplicate and
out-of-order records may repeat invalidation, but cannot move the cursor past an
unpublished or missing event. A typed local session-loss error is returned to the provider owner
through the same authority-bound callback used by ordinary calls. The renderer
reloads authoritative resources through the frozen Local API after each
invalidation.

Local doctor/repair/workspace-sync operations and Local operation logs remain
outside this release composition. The renderer therefore does not advertise
those controls. Cancellation is implemented for an existing nonterminal Local
connect/bootstrap/activation operation through its operation ID and strong ETag;
it advances the session generation, retires the operation's connection or
project binding, and ignores any late worker completion. A successful SSH check
alone still reports Core as `offline` with `core_not_started`; only project
activation can publish an online project-bound tunnel.

`core_bridge_adapters_v1.py` supplies the production adapters used by packaged
release composition. `CoreBootstrapConfigV1`
accepts composition-sealed local wheel and framework-lock paths together with
their exact byte sizes and SHA-256 digests, plus the source commit, requested
port, and replacement policy. Local paths are private in representations; no
remote service or asset path is a configuration input.

`DesktopCoreSshBridgeAdapterV1` accepts only the currently connected
`DesktopRemoteLifecycle` transport for the requested profile. Before upload it
runs a closed remote runtime selection under the same total deadline. It checks
PATH Python 3.13 through 3.11, then an owner-safe `uv` from PATH or the standard
user install locations. When neither is present on x86-64 or AArch64 Linux,
Desktop downloads the pinned official uv 0.11.28 release archive through the
profile's `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` settings. It bounds the
archive in memory, verifies the platform-specific SHA-256 embedded in Core,
extracts only the exact regular `uv` member, and executes it from a private
unlinked FD. It never executes the upstream installer script. The verified uv
then finds or installs Python 3.11 under the same deadline and proxy settings.

Every candidate is reduced to an absolute canonical path and checked as an
owner/root-owned, non-writable, regular executable before an isolated version
probe. Linux boot identity and the actual pidfd syscalls are probed directly, so
uv standalone Python does not need `os.pidfd_open` or
`signal.pidfd_send_signal` wrappers. Success returns a private content and inode
authority. Every later asset, venv, install, and Core bootstrap command reopens
that exact path without following symlinks, rechecks its metadata and SHA-256,
and executes the held FD. A pathname replacement therefore fails closed across
separate SSH processes instead of selecting another PATH interpreter.

Core generations are built under owner-only `release-staging`, not at their
final release name. A verified install lock serializes bounded recovery and
publication, while a per-stage authority lease remains inherited by ensurepip
and pip. Staged import verification is followed by an atomic no-replace rename
into `releases/<generation>`. Install failure, ENOSPC, or interruption cleans
only that inode-bound authority; unsafe cleanup leaves it for a typed
`core_bootstrap_install_failed` retry. The sidecar projects only the closed
actionable message and never remote output, paths, proxy values, or secrets.

Runtime failures distinguish no supported Python, failed uv provisioning,
unsupported kernel syscalls, and malformed/failed selection. The first two give
actionable Python or network/proxy guidance; they no longer claim Python pidfd
wrapper APIs are required.

On a supported runtime, the same transport snapshots the sealed local files
with component-wise no-follow checks, uploads them into an automatically derived
owner-only `~/.openevo/core/asset-staging` directory, and remotely rechecks file
identity, mode, size, digest, and the closed lock-to-wheel binding. Only an
atomic no-replace rename publishes the deterministic asset bundle; retries
re-verify an existing exact bundle and partial uploads remain non-authoritative.
Finalize owns an incoming transfer only after it nonblocking-locks the exact
rsync lease. A busy lease preserves the complete incoming directory and marker
for retry. Once acquired, the lease FD remains held through publication and
retirement; prepare and stale recovery count or skip active incoming slots
without consuming another process's authority.
The remaining deadline then runs the real isolated Core bootstrap/attach flow.
The returned bearer is bound to the profile plus complete remote release,
registry, generation, port, and status-proof identity with a one-way host
identity. Transport object identity is checked after preflight, staging, and
bootstrap, so reconnecting even the same profile invalidates in-flight
authority.

Proxy and TLS-bootstrap variables are passed only to uv/Python provisioning and
the isolated wheel install. The generation launcher constructs a separate
closed environment for the long-running Core service, excluding all proxy and
proxy-CA variables before `execve`.

The same adapter implements `CoreTunnelFactory`. It opens the authenticated
`ssh -W` Core endpoint through that exact transport and requires
`open_core_control_tunnel` to match `/version` and `/v1/status` to the bootstrap
attachment before publishing a bridge handle. `new_http_transport` is the
corresponding bridge `transport_factory`: every HTTP request gets a newly
verified anonymous socketpair connection and rechecks SSH child authority after
response traffic. Per-I/O tunnel timeouts are capped at the endpoint's 60-second
limit while the strict client retains its total deadline. Unknown-length request
bodies are sent only with valid HTTP chunk framing; unsupported transfer
encodings fail before wire I/O. Response `read1` delivery does not wait for a
64-KiB buffer, so small SSE frames and heartbeats are visible while a chunked
connection remains open. A generation/in-flight barrier prevents socket
adoption after close begins, cancels active connect/send/stream work, and waits
before close returns, so bearer bytes cannot be sent by a late request. The
handle's `127.0.0.1:1` endpoint is only the private HTTP origin used by the
strict client; no TCP listener is bound or reserved. Handle close delegates to
the existing bounded, idempotent, observable bridge close state and retains the
verified tunnel on timeout or callback failure for an exact retry.

`AdoptedWorkspaceArchiveSourceV1` is constructed from a frozen set of exact
`WorkspaceImportRefV1` plus private `WorkspaceImportOwnership` bindings that
composition has already durably adopted. It rejects any changed or unbound
reference before store access and calls only `WorkspaceImportStore.resolve`
with the bound ownership. The yielded object must remain the store's unlinked,
regular, read-only descriptor-backed stream; the adapter never accepts or
returns a host path, URI, or archive bytes object.

`release_runtime.py` is the single production composition owner. It derives
`openevo/wheels` from the absolute PyInstaller extraction root, opens that
directory without following its final component, and requires an owner-controlled
non-writable directory containing exactly one wheel and one canonical
`framework-lock.json`. Both files must be owner-controlled, link-count-one
regular files within their byte limits. Their bytes are read through pinned
descriptors, rechecked against their directory names, hashed, and required to
form the exact lock-to-wheel binding before any remote connection can start.
The runtime allocates `core-bridge-v1` under the private provider state root and
owns shutdown of bridge, relay, broker, and persistence.

The production workspace archive source is deliberately dynamic rather than a
startup snapshot. Under the provider reference guard, every upload read finds
the exact durable native import binding for the supplied opaque reference,
requires exactly one owning project, derives its private ownership, and only
then delegates to the verified import store. A source edit therefore cannot
leave a stale adopted-import table available to a later activation.

`core_bridge_v1.py` now provides the strict active-project bridge needed by the
next provider slice. It injects a host-global `CoreHostService`, a tunnel
factory, an opaque adopted-archive source, and a durable persistence adapter.
The bridge owns exactly one generation-linearized project tunnel and
`CoreControlClientV1`; switching or closing seals the previous client before a
new session can publish. Candidate and active generations also own tunnel,
archive-context, and blocking-adapter cleanup. Core and adapter calls pass a
generation/deadline gate before and after external work. Tunnel close is
bounded, observable, and retryable: a timeout or callback failure leaves the
handle and bridge unclosed and blocks a replacement session. A close future
that succeeds at the timeout boundary is consumed as success and is not
resubmitted. Deadline expiry immediately after submit also retains that future,
so retry waits for the same callback; only a callback exception permits a new
attempt. The tunnel factory
receives only the profile identity and remote Core port, while the bearer
remains between the host service and the strict client and is excluded from
dataclass representations and normalized errors.

The active session and activation result also bind the non-secret Local
project ID, profile ID, saved ETag, and canonical mapped-intent digest.
Capabilities, validation, and run creation receive the complete saved
`ProjectV1` and compare all four values only after entering the generation
lease. Their Core transports re-enter the same token gate, so a project edit or
session replacement cannot race a successful precheck into an old-tunnel
request. Project-ID drift and Local-version drift return distinct typed 409
errors before transport.

Activation starts from the pre-operation Local ETag, while the provider's
durable activation transaction publishes a new project ETag and observed
remote projection. `commit_local_activation()` is the only bridge transition
that may advance that Local ETag without changing mapped intent. Each
`CoreActivationV1` carries its bridge generation and a process-local,
identity-checked authority; the provider must return that exact activation as
the acknowledgement authority. The bridge accepts only an active Local
project whose Core project ID, revision, registry, model preparation, and Core
ETag exactly match that activation, then performs one compare-and-swap from
the activation's source Local ETag to the complete committed `ProjectV1`.
Only an exact retry of that committed object is idempotent. A stale generation,
substituted authority or source ETag, different committed ETag, changed intent,
or partial Core projection fails closed without changing the active binding.

Cheap project/profile/ETag checks run before canonical mapping. A Local model
that cannot satisfy Core's narrower project or archive constraints returns the
closed `invalid_local_project` 422; no public bridge method exposes a Pydantic
validation exception. Each config-dependent capability, validation, and run
call then refreshes Core project authority. The session's completed mapping
fixes canonical project intent and project/task/workspace content snapshots.
The last validated Core project may stay equal or advance through one direct
revision successor with a changed ETag, newer timestamp, and matching registry;
other mutable publication fields remain fixed. A validated successor becomes
the next predecessor. External name/spec/task/workspace drift therefore fails
before validation or run mutation even when paired with a plausible successor.

Activation negotiates version and verified capabilities, performs an exact
idempotent Core project create only when no durable mapping exists, publishes a
native-folder workspace through the bounded chunk protocol, validates the
authoritative project/head, and persists the host-bound Core mapping. Scratch
projects use Core's signed initial empty workspace. Imported projects accept
only `WorkspaceImportRefV1` and a read-only stream from the archive source; the
bridge contract contains no host path. A lost create response can be retried
only with the persisted canonical create request, its digest, and its
idempotency key. Durable create state distinguishes `pre_create`, `unknown`,
and `bound`: a proven pre-transport failure may accept a new Local action key,
unknown outcome requires exact replay, and a bound project resumes without
another create. Binding also persists the create response's complete immutable
projection: Core project ID, canonical `ProjectCreateV1`, task snapshot, and
project `created_at`. Every later GET and initial finalize must preserve that
projection. If mapping commit is interrupted and the Local draft is edited, the
bound operation first verifies the original request and immutable identity
against that Core project, then converges the new intent through a versioned
patch. For an initial imported workspace, the durable exact finalize outcome is
the revision authority at the unmapped boundary. Recovery validates its
immutable projection, project snapshots, publication, and every revision edge
through current authority before using the current ETag, issuing a patch or
upload, or committing the first mapping.

Mapped Local edits use Core `patch_project`, the freshly read project ETag, and
a deterministic old/new request key. The mapping records canonical mapped
intent, exact project/task/workspace content snapshots, and the complete
immutable projection (Core project ID, canonical project intent, task snapshot,
and `created_at`) separately from the complete mutable Core authority
projection: status, project/workspace snapshots, workspace publication, active
revision, registry digest, model preparation, project `updated_at`, and project
ETag. Every
initial-publication, mapped, patch-recovery, and finalize-recovery read uses the
same transition validator before using the current ETag. An unchanged revision
requires that complete projection, including ETag, timestamp, and registry, to
remain exactly equal. A changed revision must be the same-project direct
successor, issue a new ETag, strictly increase `updated_at`, and leave status,
snapshots, publication, and model preparation fixed; only revision, registry,
ETag, and timestamp may advance. Generation rollback, same-generation identity
rewrite, unproven generation skips, reused successor ETags, and time rollback
fail closed without workspace mutation or mapping commit. An
applied imported draft may have no outcome revision; recovery then retains its
pre-patch base revision as the effective lower bound. If both are absent, only
no revision or a same-project generation-zero revision is valid. After
capabilities, project/head agreement, and validation succeed, compare-and-swap
commit increments the mapping generation and retains the previous version in
adapter-owned history; an authority-only version may repeat the predecessor
request digest. Core must sign the required new snapshots before Desktop accepts
task, model/execution, evolution, or workspace changes.
Imported workspace upload IDs are additionally bound to the exact Core project
snapshot, so a workspace revision cannot reuse an earlier upload session. A
superseded open upload is durably aborted before its binding is cleared. Its
canonical request, digest, original ETag/key, open upload authority, and unknown
state survive restart, and every unknown result is replayed exactly before the
new workspace is uploaded or finalized.

Project patches use a separate durable `pre_patch`/`unknown`/`applied`
operation. It binds canonical old/new Local intent, patch digest and key, the
pre-patch Core project/ETag/snapshots, the validated Core outcome, and explicit
immutable-content and mutable-publication/runtime projections covering that
outcome. Reads are not used to infer an unknown result; the exact mutation is
replayed. A patch may authorize new project intent and task snapshot authority,
but its response must preserve the Core project ID and original `created_at`;
the response is rejected before applied-outcome persistence otherwise. An
imported patch may then be finalized without invalidating its
durable proof: recovery requires the persisted upload's predecessor snapshot
and ETag plus the durable exact finalize response's project/workspace snapshots
and publication before accepting current status/ETag or later successor
authority. The finalize project's active revision must descend from the applied
patch's effective lower bound and is then durable authority for later reads. A
recovered mapping may cross multiple generations only when the durable patch
base, applied outcome, and finalize outcome form a complete ordered chain of
same-revision or direct-successor edges through the current revision. Missing
intermediates do not authorize a generation jump. After an applied or finalized
outcome becomes the predecessor, a same-revision reread must equal its complete
mutable projection; a later direct successor must satisfy the same new-ETag,
strict-time, and fixed-publication constraints as mapping recovery.
Recovery performs both checks before another workspace mutation, mapping commit,
or current ETag adoption. Finalize authority CAS-persists the canonical request,
request digest, upload/project ETags, idempotency key, and complete open upload
authority before sending the mutation. Its `pre_finalize` state advances to
`unknown` before transport; response loss or restart can therefore only replay
that exact mutation. `applied` then CAS-binds the complete validated canonical
outcome and outcome digest. Reads never infer finalize success, and recovery
recomputes both bindings before reading outcome authority. An older record
without this request/outcome state machine fails closed; live Core state cannot
upgrade or repair it. Mapping CAS and matching
applied-operation cleanup are atomic. After an O-to-A patch whose
mapping commit failed, a same-A retry commits the finalized A mapping; a later
Local B edit first commits that proven A generation, then issues one distinct
A-to-B patch from A's latest ETag without another project create.

`core_bridge_store_v1.py` is the production persistence implementation of that
callback protocol. It owns a dedicated sidecar-private state directory rather
than extending the public provider database. The directory must remain a real,
owner-held mode-`0700` inode. Its database and owner-lock files are no-follow,
link-count-one mode-`0600` regular files whose device/inode identities are pinned
for the store lifetime. The database remains held by a no-follow descriptor.
Linux SQLite opens `/dev/fd/<fd>` in `mode=rw`; Darwin SQLite opens the managed
database pathname so its standard rollback journal is created beside the
database rather than under `/dev/fd`. Before configuration or schema writes, the
connection-reported inode, held FD, and managed pathname must all match. A
connect-time replacement therefore either retains the pinned Linux inode or
opens a different Darwin inode that is rejected before initialization. A
nonblocking `flock` permits one process owner, and a process-local reentrant lock
serializes SQLite use. Every public read, write, and close checks the creator PID
before it can acquire an inherited lock, so a post-fork child fails closed
without deadlocking or unlocking its parent owner. As with the provider store,
the unsigned preview does not claim protection from an arbitrary malicious
same-UID process able to race and restore the owner-private pathname between
checks.

Before SQLite receives the target database URI, the store probes a separate new
in-memory connection and requires the current library's default numeric
`synchronous` value to be `FULL`; it immediately verifies the same default on
the target connection. A non-`FULL` default fails before target hot-journal
recovery can run. The store then uses SQLite's rollback journal with explicitly
verified `synchronous=FULL`, forbids WAL/SHM, caps the database at 1 GiB and
journal at 2 GiB, and validates an exact private schema fingerprint and metadata
row in every transaction. Private persistence
schema v3 is independent of the public Core/Desktop API version. Fresh
eligibility is decided only after SQLite has recovered any hot rollback journal
through the pinned connection. The held database FD must then have zero bytes,
and that same connection must report zero pages, `user_version=0`, and no
`sqlite_schema` rows; both markers must still be unpublished. This permits a
crashed, uncommitted first schema transaction to roll back to the genuine empty
generation-zero state without trusting its nonempty pre-open size. A physically
nonempty empty-schema file, marker mismatch, failed rollback, nonempty
unversioned, v1, v2, markerless bound, or otherwise unrecognized partial store
fails closed. An eligible database may create v3 by atomically committing its
schema and exact generation-zero `pending` identity before publishing either
marker. Restart may finish only that recognized empty pending bootstrap. Its
identity fields use SQL type/byte probes and guarded reads; count/length-only
authority aggregates must prove strict emptiness within recovery capacity before
either pending digest can select authority rows.
There is no inference-based migration. Startup performs SQLite and foreign-key
integrity checks before decoding authority. Recovery is bounded to 120,000 rows
and 512 MiB of indexed/document bytes; each closed document is at most 4 MiB.
SQL length probes and exact-length guarded reads run before a document BLOB
enters Python. Mapping history is contiguous per project and bounded to 100,000
rows by default.

The database has a random non-secret store identity bound to the exact root,
database, owner lock, root marker, and external pathname anchor identities. The
mode-`0600` anchor lives in the owner-controlled parent directory and prevents a
self-consistent foreign or old root from being moved onto the managed pathname.
Both marker files use two fixed-size bounded canonical-JSON slots and retain the
latest authority generation/digest outside SQLite. Each write commits the next
database generation first, then fsyncs the root marker and external anchor. On
startup only an exact one-generation marker lag whose previous digest matches the
database proof may be completed forward. A database behind either marker, a
same-generation digest rewrite, a foreign store ID, or any physical identity
substitution is durable rollback/cross-store corruption and fails closed. For
the first generation only, the exact empty `pending` database row authorizes
retrying an empty or torn primary slot when its inactive slot remains all-zero.
A valid different marker, a dirty inactive slot, or any invalid marker after the
row becomes `bound` is not an unpublished state and fails closed.

Create, patch, mapping, abort, and finalize authority use explicit closed
canonical JSON records with a per-row SHA-256 binding. Decode strictly rebuilds
the Core DTOs and bridge dataclasses, reruns their invariants, and requires
byte-identical reserialization. The store does not use pickle or accept generic
environment, credential, secret, URI, command, or host-path fields. Create and
patch transitions compare the complete previous canonical row. Mapping commit
compares the complete create and prior mapping authority, appends the exact next
history generation with the historical create/finalize and completed-patch
transition proof, and removes only the supplied matching `applied` patch in one
transaction. Commit and startup independently require exact generation
succession, immutable same-generation revisions, direct revision successors,
monotonic timestamps/ETags, and snapshot changes authorized by a persisted
applied outcome. Exact committed-state retries resolve a lost commit response;
rollback retains the old mapping and pending patch.

Host, tunnel, archive open/read/close, and persistence callbacks run through a
fixed bounded executor. A deadline stops result delivery, while any callback
still running remains owned by the cancelled generation. Successful close or
switch waits for that work and all resources; if bounded retirement cannot
prove completion, it returns a typed retryable error instead of announcing the
transition.

`deactivate_project()` uses that serialized retirement path without
permanently closing the bridge. It is idempotent when no session exists,
rejects a different project owner or an in-progress candidate, and closes the
published client and tunnel before a later activation may start. Desktop uses
this transition when editing an active project returns it to draft. Local
activation acknowledgement uses the same transition lock, so it linearizes
before a concurrent deactivate, close, or replacement activation, or observes
that transition's newer generation and fails without touching the new session.
The release provider enters retirement in the shared session generation before
calling this method. Success clears the exact process-local binding and publishes
`offline`/`active_tunnel=false` atomically; failure retains the retirement
binding and publishes the typed error so the renderer can diagnose the lost
authority without treating the tunnel as online.

The renderer-facing run contract accepts only the active local project ID; the
later release-routing adapter must load its ETag-selected saved `ProjectV1` and
pass that complete object to the bridge. The bridge rereads Core project
snapshots, capabilities, validation, and revision head, chooses a reachable
nonterminal successor before the active head, and builds Core's `RunCreateV1`.
Core-only direct revision successors do not require a Local ETag change. Other
run, artifact, service, Core operation, log, diagnostic, maintenance, and event
methods preserve strict Core DTOs and project membership checks. Every public
bridge method exposes only `DesktopCoreBridgeErrorV1`: exact Core `ApiErrorV1`
values are retained, strict-client local errors become closed `ApiErrorV1`
values, and deferred event-iterator failures use the same boundary.

The bridge and `DesktopCoreBridgeStoreV1` are wired into the packaged release by
`release_runtime.py`. That composition owns the host service, production SSH
and HTTP tunnel adapters, adopted archive source, event broker/relay, and the
dedicated bridge-state root. It is attached to `DesktopReleaseProvider` before
the Local API advertises the complete release feature set. Missing composition
or a project without an active, generation-matched tunnel fails the affected
Core-owned route closed; startup never substitutes a mock transport, fixture,
local method table, or shared backend URL.

### Provider extension

`DesktopLocalApiProviderV1.invoke()` receives the canonical OpenAPI
`operation_id` and the already validated endpoint arguments. The release
provider has a small handler map only for implemented operations; unknown
operations fail closed. New provider operations must add verified handlers
behind this interface while keeping the decorators and signatures in
`contracts/v1/app.py` authoritative.

Provider and request-validation failures are normalized by `release_app.py`.
Error responses must remain user-safe: do not include local paths, SQLite
messages, credentials, session tokens, remote commands, or backend URLs.
Ordered request collections use JSON arrays on the Local API wire. Where a
closed sidecar model retains an immutable tuple, its request validator accepts
only an actual decoded list (or an already typed tuple for internal calls), then
validates the existing item bounds and patterns. It does not coerce strings,
mappings, scalars, or unrelated containers. This applies to
`NetworkProxyV1.no_proxy` for profile create and patch requests; tuple responses
continue to serialize as JSON arrays.

## Desktop Local event broker

`event_broker_v1.py` is the process-owned publication authority for the Local
`GET /desktop/v1/events` SSE route. Producers publish only the closed
exact `StateEventV1`, `ResourceEventV1`, or `HeartbeatEventV1` models; subclasses
are rejected even if they inherit the same fields. The broker snapshots the
accepted frozen model through strict Python validation, preserving tuple and
other model-only types before the JSON boundary. It then adds one monotonic
JavaScript-safe sequence, a sequence-bound opaque event ID, the canonical event
name, and a UTC timestamp, then serializes and bounds the canonical
`EventEnvelopeV1` frame exactly once.
The retained ledger and subscriber queues hold only that immutable event ID and
frame bytes. Later mutation of a producer-owned or returned model therefore
cannot rewrite replay or live delivery. A rejected timestamp, model, identity,
or oversized SSE frame consumes neither sequence authority nor replay capacity.
Clock and event-ID callbacks run outside the broker state lock. A publication
linearizes only in its final locked commit, where it rechecks close state,
assigns the next sequence, and atomically updates replay and subscriber state.
Callback failure consumes no sequence; same-thread recursive callback entry is
rejected, while synchronous cross-thread callback publication can reach its own
commit without deadlocking. If `close()` commits while a callback is running,
the pending publication fails without consuming a sequence.

The retained ledger is bounded by both event count and total frame bytes. Its
defaults are 4,096 events and 16 MiB; configuration cannot exceed 100,000 events
or 256 MiB. A successful publication evicts complete oldest frames until both
bounds hold. One frame that cannot fit the configured ledger fails before
commit. Subscriber queues retain shared frame references but are charged their
logical bytes: all queues together default to at most 16,384 references and
64 MiB, with hard limits of 262,144 references and 256 MiB. Each queue remains
independently bounded, and the default total subscriber limit is 256 with a
hard limit of 4,096. If live fanout would cross a per-subscriber or global queue
bound, that subscriber's pending references are released and it receives a
terminal gap. Replay admission that cannot fit the global queue budget fails
synchronously before the stream response starts.

A subscription with no `Last-Event-ID` starts at the live head; an exact
retained cursor replays only later records and registers for live delivery under
the same lock. Unknown, evicted, or too-old cursors fail synchronously before
the stream response starts. Cancellation of a pending `__anext__`, stream
disconnect through `aclose()`, and abandoned-subscription GC all unregister the
subscriber and release its queue charges. Idle streams emit an SSE comment
every 15 seconds, which carries no sequence or replay authority. Closing the
broker atomically prevents publication, clears all memory charges, and
terminates all existing subscriptions.

`DesktopReleaseProvider` returns a `StreamingResponse` over this subscription
when the broker is part of release composition. It uses no-cache/no-buffering
headers, rejects an unknown cursor before sending HTTP 200, and closes the
broker before bridge and store shutdown so connected renderers terminate. The
provider's state-change publisher emits the complete current `DesktopStateV1`;
it remains an invalidation signal, and the renderer still reloads authoritative
resources.

This module does not infer resource state or cache partial Core payloads. The
release composition remains responsible for mapping validated Core events to
ETag/digest-bound Local invalidations and for publishing Desktop-owned state and
operation changes. The renderer responds to those invalidations by reloading
the authoritative resource snapshot.

## Core Control API v1 Client

`core_client_v1.py` is the strict post-bootstrap transport from the Desktop
sidecar to remote Core. A `CoreTunnelConnectionV1` is valid only for one active
project session and one explicit `http://127.0.0.1:<port>` or
`http://[::1]:<port>` SSH-tunnel origin. The caller must issue its bearer with a
CSPRNG at 256 bits or stronger and must replace the connection when the active
project session changes.

The client creates its own `httpx.Client`; tests may inject only a transport.
Environment proxy discovery and redirects are disabled. Discovery calls are
unauthenticated, while every `/v1` request attaches the bearer only to the
fixed origin. The client first validates and pins one release `openevo_core`
`/version` response whose OpenAPI digest is exactly
`0553a38f229c4fe091b29c609c7557e12d0d30354170d19ba8377da04469ee48`.
Every authenticated `/v1` call fails before transport until that negotiation
succeeds; simulator, scaffold, dry-run, development, and changed release
identities are rejected. Mutations require their contract-declared idempotency
and ETag precondition headers. Public list methods expose only each route's
closed query set and runs are always filtered to the active project.

Core owns newly created project IDs, while an ordinary `CoreControlClientV1`
is already bound to exactly one project. New-project setup therefore uses the
narrow `CoreProjectBootstrapClientV1`: after the same release `/version`
negotiation, it may submit one idempotent `ProjectCreateV1`, verifies that the
returned draft preserves every request-owned field and the fixed initial
workspace/revision shape, and returns a
new `CoreTunnelConnectionV1` bound to Core's generated ID. An exact replay of a
delivered success is local. The first request and idempotency key are frozen
before transport, so an unknown network outcome can only be retried exactly;
a different request or key is rejected even when no response was delivered.
Initial draft validation rejects an already published imported workspace and
requires the documented scratch/imported workspace snapshot shape. Result
validation, connection binding, replay-state commit, and delivery share the
same generation barrier as `close()`; lock wait and transport share one deadline.
The project-bound client rejects `create_project` before transport, preventing
an orphan project followed by an active-project mismatch.

Requests are exact Pydantic v1 DTOs. JSON responses and `ApiErrorV1` bodies are
read with route-class byte limits before contract-model validation. A generic
model-generated JSON Schema pass recursively rejects scalar coercion and
unknown object fields before Pydantic validation while preserving JSON arrays
as valid encodings of tuple fields. The first valid capabilities response pins
the client lifetime's exact release execution profile and registry digest.
Later capability reads, project validation requests/responses, project
snapshots, and run requests/responses must match that authority, in addition to
the run snapshot and required-revision bindings. Capability and cached Core
project registry digests are compared exactly regardless of which response
arrives first; a missing project digest does not match a pinned capability
digest. Malformed, oversized,
redirected, cross-project, or connection failures become closed local errors
without raw bodies, headers, URLs, paths, or credentials.

Every client requires a finite positive timeout, and every component of an
`httpx.Timeout` must be finite. The timeout is also the hard wall-clock budget
for the complete public operation, capped at 300 seconds. The same deadline
covers transport send, redirects, bounded JSON/error reads, nested client calls,
and the full SSE stream window; trickle traffic cannot renew it. Synchronous
transport calls run on one process-wide, fixed eight-thread daemon executor, so
a transport that ignores cancellation cannot create unbounded owner threads.
Queued work is cancelled at deadline when possible, and a late response is
closed through the bounded resource closer. Mutations are submitted exactly
once and are never replayed automatically after timeout or connection failure.

The shared HTTP client is safe for concurrent calls; `close()` is idempotent and
immediately seals the client against new leases. Every response and transport
close, including ordinary response-context exit and a response that arrives
after sealing, is submitted outside the state lock to one process-wide bounded
queue served by exactly four prestarted daemon workers. Creating more clients
does not create closer or ownership threads. An uninterruptible synchronous
close cannot exceed the caller's wait bound. Each client transport and each
outbound response reserves one globally bounded close-ownership slot before
network I/O. The reservation makes the later close submission non-droppable;
when capacity is exhausted, the next request fails before transport. Failed
close actions permanently seal that client against new leases, and an
unexpected failed submission remains client-owned for bounded retry. A closed
connection cannot send its bearer after Desktop switches to another project
session or tunnel.

The close seal increments a client session generation. Each public JSON call owns
one generation token and a copy-on-write authority/cache transaction. Network
I/O, bounded body reads, response-model validation, nested public calls, and
cache validation do not hold the close state lock. After all validation succeeds,
generation admission surrounds the copy-on-write cache transaction. On the
transaction's final exit, one delivery-barrier critical section shared with
`close()` performs the deadline/generation check, cache commit or rollback,
delivery linearization, and lease release. If the seal starts first, the
transaction rolls back and the pending return is replaced with
`core_client_closed`; if delivery commits first,
close linearizes after it. `close()` need not wait for a stalled request thread,
and after it returns no uncommitted result from the sealed generation can be
delivered.

SSE parsing and cache validation likewise happen outside the close state lock.
The stream is an explicit iterator; every `__next__` owns one generation
admission around one replay-ledger/cache transaction. Its final exit uses the
same atomic delivery-barrier commit and lease release as JSON. A seal that wins
replaces the pending return with `core_client_closed` and rolls back replay
authority. `close()` returning is a hard boundary: no uncommitted old frame may
be yielded afterward.

Before URL/request construction, path segments (including their decoded form),
query values, cursors, caller-provided headers, and decoded request bodies are
recursively checked for the bearer, fixed Core tunnel URL/origin, and private
Desktop session identity. The active project identity is checked when the
connection is created. The same recursive check applies after JSON/error/SSE
decoding, so percent or JSON Unicode escapes cannot bypass credential
sanitization or place private values in a request URL/access log. Release providers currently generate
`Idempotency-Key`, `Last-Event-ID`, and SSE `id` values as visible ASCII. The
client rejects non-ASCII or control characters instead of percent-encoding
them; this is a temporary release implementation constraint, not a broader Core
opaque-ID contract change.

Core SSE declares `SseFrameV1` as its wire contract. The client bounds each
frame and each reconnectable stream window, accepts only `id`, `event`, and
`data`, validates `data` as the closed `EventEnvelopeV1`, and then strictly
validates the complete `SseFrameV1` before yielding it. A client-lifetime,
bounded ledger binds every SSE ID to the canonical validated event digest across
reconnects. Exact semantic replays are accepted even if JSON formatting differs;
after their canonical digest matches, they are no-ops and do not reapply
authorization or resource state. An ID reused for different event data, or a
ledger that reaches its bound, fails closed. The client does not reconstruct event payloads. Workspace publication,
document-change artifact diffs, and
operation request/result/cancellation are likewise validated only through
their Core-owned response models. Strict project, run, service, artifact,
operation, and diagnostic reads establish opaque project-membership bindings.
Operation and diagnostic identity, parent membership, and every log reference
are validated under one lock before any authorization cache entry is committed.
Status and paginated project, run, service, and artifact snapshots validate into
temporary cache copies and publish as one update; a late invalid item leaves no
membership or resource-cache residue.
Events without a direct project identity are yielded only when their declared
run or service parent is already bound to the active project; otherwise the
stream fails closed with snapshot-refresh-required semantics.

Artifact diff authorization is current-summary lineage plus the predecessor
identity/digest carried by the strict Core diff response. Default diff never
re-fetches the historical predecessor through the current-only detail route;
an optional cached predecessor can only add an equality check. Artifact content
and diff have a dedicated 32 MiB response bound so every legal 2 MiB Core text
payload, including NUL/newline/Unicode worst-case JSON escaping and maximum
closed diff structure, remains receivable without widening other JSON routes.

Workspace upload snapshots bind each strong ETag one-to-one to one canonical
representation for that upload: neither the same ETag with different state nor
the same state with a different ETag is accepted. Offset, status, and update
time cannot move backward. A newly created upload must issue an ETag distinct
from the project `If-Match`; an exact idempotent replay of the complete create
response may retain its upload ETag. Chunk, abort, and finalize responses change
upload state and therefore must issue a new upload ETag. Finalization
independently requires the returned project ETag to differ from the upload's
frozen project ETag.

Durable stale-upload abort recovery uses the public generation-bound
`abort_persisted_workspace_upload` client operation. Exact persisted authority
restore, upload ETag and idempotency validation, abort transport, cache update,
and return delivery are one copy-on-write transaction. If close seals that
generation before delivery, neither the open authority nor the abort result is
retained; the bridge never calls the client's private upload registration helper.
