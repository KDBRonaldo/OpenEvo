# Desktop sidecar

The sidecar owns Desktop-local security boundaries that must not be exposed to
the React renderer. Local HTTP routes and the native host are separate adapters;
they are not implemented in every private sidecar service module.

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
idempotency key. The
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
renderer can request native `adopt` or `discard` by action ID but never receives
the token or hidden route. Create/patch commits adopt only after project state is
durable. Close, reselect, reset, stale picker completion, and failed save paths
discard. Discard first takes the provider reference guard, rereads all durable
project references, and only then takes the import lock, so it cannot remove an
import concurrently committed by another request. Referenced pending imports are
adopted during startup recovery; unreferenced leases are removed. Total retained
limits remain 10,000 imports/24 GiB, while pending state is separately capped at
64 imports and 16 GiB by default.

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

The private import and discard routes are excluded from OpenAPI and require a process-owned native
handoff token that is distinct from the renderer's Desktop session token. Its
bounded import request is the only path-bearing message; its private success
envelope contains `ProjectSourceV1` plus the native-only lease and never echoes
that path. Rust strips the lease before returning to React. Native
import ownership is reproducible from project identity and archive digest. A new
project created from a native import receives a deterministic project ID derived
from the opaque import ID; an existing project supplies its own ID privately.
The release provider verifies the exact import and ownership before persisting a
native project source. Project reference mutations and import cleanup use one
fixed lock order: provider reference guard, then import-store root lock. A source
patch compares `import_ref` identity rather than display metadata. Post-commit
cleanup reacquires the guard, rereads the complete durable reference snapshot,
and releases the old exact import only if its ID is still unreferenced. Startup
uses the same coordinated snapshot and retains only exact reference/ownership
matches while removing abandoned picker imports. Core upload consumption remains
the next integration step.

## Release Local Provider

The sidecar also owns the renderer-facing Desktop Local API and the process-owned
connection to remote OpenEvo Core. The canonical public contract is defined once
in `contracts/v1/app.py`; release implementations must use its provider injection
point instead of registering another route table.

### Current implementation

`release_app.create_release_desktop_local_api_app()` creates the real Local API v1
application. It owns one `DesktopProviderStore` and one `WorkspaceImportStore`
for the process lifetime and
requires the native host to supply a Desktop session token, native instance ID,
readiness key, source commit, and private state root.

This phase implements:

- public `GET /version` with `provider_kind=desktop_sidecar` and the canonical
  OpenAPI digest;
- challenge-bound `GET /health` using HMAC-SHA256 over
  `protocol NUL instance_id NUL challenge`;
- constant-time Desktop session authentication for every `/desktop/v1/*` route;
- `GET /desktop/v1/state` with an explicit disconnected Core state;
- profile and project list/create/get/patch/delete through
  `DesktopProviderStore`, including durable idempotency, signed cursors, ETags,
  and restart recovery.
- private identity-bound native folder import, project-source verification, and
  committed/startup lifecycle cleanup;
  this route is deliberately absent from the public Local API OpenAPI document.

No SSH or Core operation is claimed by this slice. Connection actions,
activation, validation, operations, runs, artifacts, services, diagnostics,
maintenance, and events return a closed `ApiErrorV1` with HTTP 503. They never
return fixture data or a synthetic ready/success state.

### Provider extension

`DesktopLocalApiProviderV1.invoke()` receives the canonical OpenAPI
`operation_id` and the already validated endpoint arguments. The release
provider has a small handler map only for implemented operations; unknown
operations fail closed. Later SSH and Core providers should add verified
handlers behind this interface while keeping the decorators and signatures in
`contracts/v1/app.py` authoritative.

Provider and request-validation failures are normalized by `release_app.py`.
Error responses must remain user-safe: do not include local paths, SQLite
messages, credentials, session tokens, remote commands, or backend URLs.
