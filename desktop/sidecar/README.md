# Desktop sidecar

The sidecar owns Desktop-local security boundaries that must not be exposed to
the React renderer. Local HTTP routes and the native host are separate adapters;
they are not implemented in every private sidecar service module.

## Workspace imports

`WorkspaceImportStore` is the private persistence and verification layer for a
native-folder snapshot. Its ingest boundary accepts only an already-open,
seekable regular-file descriptor or a binary stream backed by such a descriptor.
It does not accept a host path, URI, archive bytes object, or renderer payload.
The future Tauri native channel is responsible for opening the picker result
without following a symlink and privately transferring the open descriptor to
the sidecar.

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
metadata files are mode `0600` and link-count one. A random opaque import ID is
independent of the source name and content digest. Archive plus metadata publish
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
It removes only recognized temporary/unknown entries and deterministically proven
structural or content corruption without following symlinks. Filesystem and xattr
`OSError` failures are treated as infrastructure failures: reconciliation keeps
the observed entry and fails closed for a later retry.

The only ingest result is the existing closed contract model:

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

The native descriptor transport, picker lifecycle, Local HTTP/provider wiring,
and Core upload consumer remain future integration work. The provider owns the
mapping from frozen Local API project/operation context to the internal ownership
value described above.
