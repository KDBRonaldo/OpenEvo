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

The store root and import directories are mode `0700`; archive and closed
metadata files are mode `0600` and link-count one. A random opaque import ID is
independent of the source name and content digest. Archive plus metadata publish
as one fsynced directory through an atomic no-replace rename. Metadata binds the
exact `WorkspaceImportRefV1`, private storage identity, and a random archive
generation token. Startup reconciliation is node/byte bounded and removes
temporary, malformed, tampered, and unknown flat entries without following
symlinks.

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

`resolve(import_ref)` reopens metadata and archive with no-follow semantics,
binds their identities, rehashes the archive twice, and yields a context-managed
read-only binary handle whose `name` is an integer descriptor. It never returns
a host path. `release(import_ref)` and `delete(import_ref)` remove only an exact,
verified reference. Tamper, replacement, unsafe root state, unavailable atomic
rename/xattr support, and reconciliation budget exhaustion all fail closed.

The native descriptor transport, picker lifecycle, Local HTTP wiring, project
reference ownership, and Core upload consumer remain future integration work.
