# OpenEvo Desktop Release Packaging

> Pre-release status: final External Beta publication remains disabled. The
> stable-only manual workflow builds and validates an unsigned candidate, leaves
> it as a draft GitHub prerelease after upload/download verification, and never
> signs, notarizes, or publishes that draft.

The canonical release requirements live in
`docs/maintainer/productization/spec.md`. This document defines the packaging
boundary that workstream C3/D3 must implement.

## Product Boundary

OpenEvo Desktop is the ordinary-user macOS application. Its package combines:

- the React/Vite user interface;
- the Tauri/Rust native host;
- the bundled local sidecar;
- the descriptor-matched OpenEvo Core install artifact used for remote
  bootstrap.

The browser-served Vite build is a development and CI surface, not the released
application. The Core wheel is a remote backend artifact, not a second desktop
application.

## Current Implementation

The repository currently provides:

- Tauri configuration under `desktop/src-tauri/` with a `dmg` bundle target;
- React build scripts under `desktop/package.json`;
- `desktop/packaging/build_sidecar.py` for producing the external sidecar
  binary. Each invocation copies only `pyproject.toml`, `README.md`, `LICENSE`,
  and `src/` into an exclusive temporary directory, excluding wheel, egg-info,
  and cache content. It builds the exact Core wheel there with the locked
  `setuptools` and `wheel` using `python -m build --no-isolation`, verifies its
  name/version metadata, then uses Core's authoritative
  `FrameworkDistributionLock` model and loader to write and reload a canonical
  `framework-lock.json` bound to that wheel's filename, version, and SHA-256.
  PyInstaller receives both exact files with `--add-data` at `openevo/wheels`.
  Archive inspection requires that directory to contain exactly one matching
  wheel and one lock, verifies raw CArchive TOC multiplicity before accepting
  PyInstaller's parsed inventory, verifies the wheel bytes, and reloads the
  embedded pair through the same lock contract. `--core-wheel-output-dir`
  publishes those same verified bytes as one atomic directory for release
  workflows. The requested output path must not exist. The builder opens
  the generated wheel and lock without following symlinks, revalidates their
  identity and bytes, copies them as owner-only regular files into a private
  random sibling directory, fsyncs both files and that directory, and verifies
  the exact two-member inventory. It then publishes the complete directory with
  an atomic no-replace rename and fsyncs the parent. A pre-publication failure
  never creates the requested output; random staging residue is non-authoritative
  and is not automatically adopted or deleted. A post-publication retry fails
  because the output already exists. This contract is for a GitHub-hosted
  ephemeral runner or an equivalently controlled one-shot build account. It does
  not claim isolation from malicious code running under the same build UID and
  provides no persistent-workspace recovery protocol. Core wheel construction
  fixes `SOURCE_DATE_EPOCH` to the trusted source commit time, while the canonical
  lock and SHA-256 checks bind the exported pair to the bytes embedded in the
  sidecar. After all archive checks pass, the builder copies the externalBin to
  a private random sibling file, fsyncs and verifies its exact bytes and mode,
  then atomically replaces the Tauri target and fsyncs the binary directory. A
  failure before replacement preserves the previous target and leaves only a
  non-authoritative staging file for inspection.
- source-level frontend, sidecar, Rust, and package-inventory tests;
- Linux and macOS CI jobs that build the actual PyInstaller externalBin and
  exercise it through the production Rust native-launch path;
- `.github/workflows/openevo-release-smoke.yml`, whose macOS packaging job is
  the sole producer of the exact Core wheel and canonical
  `framework-lock.json` used by that workflow. The job verifies a two-entry
  SHA-256 manifest, exports its digest as a job output, and uploads the pair and
  manifest under an Actions artifact name qualified by source commit, workflow
  run, and run attempt. The dependent
  Linux Core job downloads that exact artifact, verifies the transferred
  manifest against the producer's digest and then verifies both members before
  install. It does not rebuild either release input. The Linux job owns the
  actual `openevo-core-service` lifecycle and frozen registry identity smoke;
- `.github/workflows/openevo-desktop-candidate.yml`, a stable-only manual
  candidate workflow. Its macOS job uses locked inputs, clean-installs the exact
  embedded Core wheel, runs renderer and release-mode Rust checks, and builds
  only the architecture reported by `rustc --print host-tuple`. Both the raw app
  bundle and the app copied from the mounted DMG launch the real
  `Contents/MacOS` Tauri executable. The smoke records a visible renderer
  window, packaged sidecar readiness, inherited listener FD 3, executable FD 4
  matching the bundled externalBin bytes, and bounded process-group cleanup.
  The dependent Linux job downloads the complete candidate manifest and
  installs, framework-verifies, and service-smokes the same final Core wheel
  bytes. Only after both jobs pass does a write-scoped job create an unsigned
  draft prerelease, upload every manifest member, download every asset into an
  empty directory, revalidate the closed inventory, compare all bytes, and
  validate the review-facing draft fields and the per-attempt random ownership
  marker. A separate run-attempt-qualified Actions artifact retains that
  point-in-time metadata record. The successful draft has a `tagName` but does
  not create a remote Git tag;
- a disabled `.github/workflows/openevo-release-artifact.yml` placeholder that
  publishes nothing.

The candidate workflow proves the native packaging, exact-byte transfer, Core
service, minimal dependency/license/security, and draft-asset roundtrip named
above. It does not complete the ordinary science E2E, benchmark gates,
secret-canary/privacy suite, code signing, notarization, or final publication,
and the draft's candidate tag name is not a real Git tag or public release
authorization.

### Native host trust boundary, phase one

Part of #158 establishes the first release-only native-host boundary. A release
build derives the sidecar source path only from the current application
executable and the `bundle.externalBin` basename. Every bundle directory
component is opened relative to a held directory FD with `O_NOFOLLOW` and must
be owned by root or the effective user. Linux rejects group/world-writable
components except a root-owned sticky boundary such as the system temporary
directory. macOS additionally permits a root-owned, group-writable component
that is not world-writable; this covers the standard root:admin `0775`
`/Applications` directory. User-owned group-writable components and every
non-sticky world-writable component remain invalid. Set-user-ID execution is
rejected by requiring the real and effective UID to match.
Darwin's fixed `/var` and `/tmp` aliases are mapped to `/private/var` and
`/private/tmp` before this traversal. The native host does not call `realpath`
or accept any other symlink; every mapped component is still opened from the
root FD with `O_NOFOLLOW`.

On macOS, mode and owner checks are not the complete write policy. Native code
reads the extended ACL from every held component FD and from the held sidecar
source FD with `acl_get_fd_np`. A NULL result with `ENOENT` means the held file
has no extended ACL and is accepted; every other NULL result fails closed.
Enumeration follows Darwin's `acl_get_entry` contract, where zero returns an
entry and `-1/EINVAL` marks the end. A malformed entry, unknown tag, unknown
ALLOW permission, or ALLOW entry containing write-data, append, delete,
delete-child, write-attribute, write-extended-attribute, write-security, or
change-owner permission fails closed. Read/execute-only ALLOW entries and DENY
entries are accepted,
so the standard deny ACLs and root:admin `0775` mode used by `/Applications`
remain supported. This deliberately stronger policy rejects a mutating ALLOW
entry even when its principal might otherwise be trusted. Linux keeps its mode,
owner, and no-follow policy and does not apply this macOS ACL interpretation.

Linux obtains the loaded executable vnode through `/proc/self/exe`; its owner
must be root or the effective user, and the sidecar source owner must match it
exactly. macOS has no equivalent interface that reopens the vnode already
loaded by the process. The macOS policy therefore does not claim that reopening
`current_exe()` authenticates the loaded image: it accepts a sidecar owned by
root or the effective user only after the no-follow component policy above.
This explicitly supports root-owned and user-owned bundles while rejecting a
third UID, including the normal drag-to-`/Applications` installation. Allowing
root-owned group-writable macOS components does not turn a replacement into a
trusted file: each opened component must still be root/effective-user owned,
the retained no-follow FDs anchor subsequent traversal, and the final source
must independently be root/effective-user owned, link-count one, and not
group/world writable. A same-UID process remains inside this phase-one trust
boundary.

The source must be a non-empty, link-count-one regular executable that is not
group/world writable. Native code hashes the held source FD before copying,
copies and hashes it, then hashes it again. Before and after those reads it
requires the source FD and parent-relative pathname to retain the same device,
inode, size, mode, link count, owner, mtime, and ctime. All three digests must
match. The private destination is created exclusively in a native-created
`0700` directory, opened once for writing and separately read-only, and
inode-bound unlinked before use. After `fsync` and mode `0500`, writer and reader
identity, size, link count zero, and digest are checked again; only the read-only
FD survives. The `0700` directory is not treated as protection from another
same-UID process. Its owner retains a directory FD and performs only an
identity-checked, non-recursive `rmdir`, so pathname replacement cannot cause
recursive deletion.

Linux execution and archive reads use inherited FD 4 through `/proc/self/fd/4`.
On macOS, archive reads still use verified FD 4 through `/dev/fd/4`, but the
PyInstaller onefile parent cannot use that device path for its later child
`execvp`. The native host therefore supplies a second non-secret, closed
environment field, `OPENEVO_NATIVE_EXECUTABLE_PATH`, containing only the private
`.../openevo-desktop-sidecar` pathname whose final inode was matched to FD 4 in
the Rust `pre_exec` hook. `OPENEVO_NATIVE_EXECUTABLE_FD` remains exactly `4`.
Both names must be removed from the inherited environment allowlist before the
native host sets its own values; Linux must leave the pathname field absent.

The custom bootloader treats the two values as one protocol. A path without the
FD marker, an FD value other than `4`, a macOS FD launch without the path, or a
Linux FD launch with the path fails closed. On macOS it requires an absolute
path with the fixed basename and no control or dot-segment components, then
compares both the original and resolved pathname `lstat` identities with
`fstat(4)`, requires a link-count-one regular file, rejects group/world write,
and accepts only root or effective-user ownership. It resolves parent-component
aliases such as macOS `/var` to `/private/var` within those checks.
`pyi_ctx->executable_filename`
receives that resolved private path solely for onefile child execution;
`_pyi_main_resolve_pkg_archive` independently opens
`/dev/fd/4`, so parent and child archive bytes remain FD-bound. The packaged
Python entry point removes both protocol fields before launcher `main` runs.

The sidecar builder downloads only the exact size/SHA-256 PyInstaller sdist
recorded in `uv.lock`, applies exact-source resolver and archive patches, and
rebuilds it. The parent validates private FD identity and digest before and after
spawn, and the Rust child validates its inherited FD against the private
parent-relative pathname immediately before exec. Typed failures contain a
stable code and user-readable message without either host path. The retained
macOS pathname is still inside the phase-one same-UID trust boundary: a same-UID
process can race replacement after identity validation and before a later
pathname-based `execvp`. Code signing or notarization alone does not close this
pathname TOCTOU, and this design does not claim otherwise.

Every verified packaged launch also removes all inherited environment names with
the PyInstaller-private `_PYI_` prefix and forces
`PYINSTALLER_RESET_ENVIRONMENT=1`. This prevents an inherited extraction path,
archive identity, parent level, splash endpoint, or future private bootloader
field from selecting attacker-controlled state. Reset is a first-bootloader
instruction: the bootloader consumes it while constructing the clean packaged
environment, so ordinary subprocesses created by the running sidecar inherit the
normal post-bootloader environment rather than another forced reset.

The native host binds the loopback listener before spawn and transfers that
already-bound socket on inherited FD 3, removing the release-and-rebind port
window. Native code sends exactly one UTF-8 JSON frame of at most 512 bytes over
the child's stdin and then closes the pipe. Its exact keys are `protocol`,
`instance_id`, `readiness_key`, `session_token`, and `handoff_token`; protocol is
`openevo-native-sidecar-v1`, instance ID is 128 fresh bits, and all three
credentials are independently generated 256-bit values. Duplicate, missing, unknown,
malformed, or trailing input is rejected by the strict sidecar integration.

The custom bootloader validates FD 3 before every onefile handoff and after
descriptor restoration. Both platforms require a socket descriptor and a
regular FD 4 archive. Linux reads `SO_ACCEPTCONN` directly. macOS, where that
getter is not available, uses the system `libproc` API
`proc_pidfdinfo(PROC_PIDFDSOCKETINFO)` and requires an IPv4 TCP stream with
`SO_ACCEPTCONN` in the kernel-reported socket options. It then uses
`getsockname` to require a non-zero `127.0.0.1` endpoint. The audited
PyInstaller source patch links `libproc` only for Darwin; an unavailable API,
short structure, wrong socket kind, non-listening socket, or non-loopback
endpoint fails with a closed startup diagnostic rather than falling back to a
pathname or rebinding a port.

The packaged Python launcher applies the same 512-byte bound, requires the
closed five-key object and lowercase fixed-width hex values, and passes the
readiness/session values to `create_release_desktop_local_api_app` while retaining
the handoff credential only for hidden native workspace routes. It
mounts only that release Local API and the audited product web. It does not
construct the legacy sidecar app, expose `/openevo-api/*`, translate the Desktop
session header into a legacy mutation token, or accept a backend base URL. Its
durable provider state is isolated under `<Desktop config root>/local-api-v1`.
SQLite may report an OS-canonical spelling of that database path, including the
macOS `/var` to `/private/var` alias. The provider therefore requires an absolute
`PRAGMA database_list` path and requires the SQLite-reported path and managed
pathname to share one verified device/inode before and after connection
configuration. This preserves SQLite's normal same-directory rollback-journal
and hot-journal recovery behavior while accepting inode-identical ancestor
aliases. A different inode, unsafe file metadata, or changed managed pathname
fails closed; pathname string equality is not an authority check. The state root
is owner-private and process-locked. An arbitrary malicious process already
running as the same macOS user and able to rewrite that private root is outside
the unsigned preview threat boundary.
Provider binding traverses both materialized FastAPI routes and deferred included
routers, while preserving every frozen endpoint signature. This keeps the same
Desktop Local API provider contract across supported FastAPI router
representations instead of silently leaving nested product routes contract-only.
The hidden `/openevo-native/session` probe accepts exactly one matching
`X-OpenEvo-Desktop-Session` value and returns an empty 204; missing, duplicate,
or incorrect values return 403. The probe is excluded from the frozen public
Local API schema. Frame credentials are not logged or included in exception
text.
None of these values is placed in argv, environment, or a file. The readiness
key and Desktop session token are never returned by HTTP discovery, native
status, or logs; only `start_sidecar` returns the session token directly to the
renderer. The non-secret instance ID is returned only as part of the
challenge-bound health response.

Failures before readiness use the local-only `OPENEVO_STARTUP_V1` diagnostic
contract. The bootloader and packaged Python entry point emit a closed, fixed
`stage` and `code`, plus an optional numeric errno, for handled startup failures.
Only those exact allowlisted records are eligible for surfacing: paths, argv,
environment values, credentials, URLs, exception messages, tracebacks, and
arbitrary child output are never copied into CI logs. The release smoke uses a
bounded OS pipe rather than a disk-backed process log, scans at most 32 KiB, and
reports at most eight records. One additional byte is read only as a truncation
sentinel and is never parsed or rendered. Missing diagnostics are reported as
such. This is a local startup aid, not telemetry, crash reporting, or a
diagnostics upload path. Python release composition tracks a closed phase set
for the embedded Core pair, provider and workspace stores, SSH lifecycle, Core
bridge/runtime, Local API provider/routes, and static app. It converts only the
last phase, including native routing and native-frame/listener/server startup,
into an allowlisted `python_launcher/*_failed` code. Exception types, messages,
chained causes, and runtime values are not part of the contract.
Provider and listener cleanup is attempted on every server exit. A cleanup
failure without an earlier server failure becomes the fixed, redacted
`python_launcher/shutdown_failed` diagnostic; cleanup errors never replace an
already selected startup or server phase code.
The packaged launcher disables the Local API app's optional ASGI shutdown close
hook and is the sole owner of provider shutdown. This avoids Uvicorn converting
a provider cleanup exception into internal lifespan state before the launcher
can fail closed. Directly embedded Local API apps retain the shutdown hook by
default and must opt out only when their host assumes the same explicit owner
role.
The packaged launcher also defers Uvicorn's replay of `SIGINT`/`SIGTERM` until
the server has returned and explicit provider/listener cleanup has run. Signals
received in the small pre-capture window set `Server.should_exit` rather than
being lost. Development/test launchers keep Uvicorn's default signal behavior
and their ASGI shutdown hook.
Release app and Core-runtime construction use the same primary-failure rule:
attempt every cleanup that has acquired ownership, suppress secondary cleanup
exceptions, and propagate the original construction failure.
The runtime's ordinary `close()` similarly attempts relay, bridge, broker, and
bridge-store cleanup independently and propagates the first cleanup failure
after all attempts complete.
Runtime stop/close is linearized across callers, and provider close preserves
the first failure while independently attempting executor, runtime, lifecycle,
provider-store, and workspace-store cleanup.

The smoke keeps the launched sidecar leader unreaped while health is pending and
uses the Core non-reaping process-group observer for exit and cleanup. Normal
cleanup first proves that the complete PGID has no live members, then reaps the
leader and proves the PGID absent. If that observation authority fails, the
still-unreaped leader authorizes a direct whole-group `SIGKILL` fallback and
leader reap; the candidate remains failed even when this emergency cleanup
succeeds. The launch owner also covers native credential-frame handoff, so
cancellation or any other `BaseException` during the write/close interval cleans
the owned process group before the exception propagates.

Readiness sends a fresh 256-bit challenge to `/health`. The closed Rust response
model requires the exact protocol and instance ID and verifies HMAC-SHA256 over
`protocol NUL instance_id NUL challenge`. Unknown fields, stale challenge
proofs, arbitrary HTTP 200 responses, invalid UTF-8, and responses over the
fixed byte limit cannot satisfy startup readiness. After identity proof, native
code reads the sidecar's actual `/version` response into a deny-unknown-fields
model and requires Local API major 1, release provider `desktop_sidecar`, the
frozen OpenAPI digest, and a unique subset of the closed feature-flag enum. The
expected digest has one build-time source of truth,
`DESKTOP_LOCAL_API_OPENAPI_SHA256`; it contains the final frozen Local API v1
OpenAPI digest. Exact bootstrap and version tests consume that same native
constant.
The release contract requires only `remote_profiles`, `project_validation`,
`operation_events`, `run_observability`, and `artifact_inspection`.
`service_control` and `diagnostics` remain valid future enum values but are not
advertised by this release because their Core owners are unavailable.
Native readiness also requires `GET /openevo-api/desktop/shell` to return 404,
so the old shell token route cannot remain in a release sidecar inventory.
It then calls the hidden no-side-effect native session probe with
`X-OpenEvo-Desktop-Session` and requires an empty 204 response, repeats the same
probe without the header, and requires 403. Only after both results prove the
sidecar is bound to the frame token may native code publish endpoint and token
to the renderer. Contract, digest, provider, feature, route, or session-binding
mismatch triggers owned child-group cleanup and startup fails closed.

`start_sidecar` and lifecycle status are separate renderer contracts.
`start_sidecar` returns exactly `DesktopBootstrapContextV1` with keys
`schema_version`, `endpoint`, `session_token`, and `negotiated_contract`.
`endpoint` is the loopback origin without a path. `negotiated_contract` has
exactly `major`, `openapi_sha256`, `provider_kind`, and `feature_flags`.
`host_status` and `stop_sidecar` return only `{state}`; they never expose a PID,
port, URL, endpoint, command, or credential. Internal lifecycle snapshots retain
the process data required for ownership but are not serializable renderer DTOs.
The renderer caches this bootstrap context while requests can reach the endpoint.
Ordinary request/response calls have a 15-second bound covering both `fetch` and
response-body consumption. A network `TypeError` or this internally generated
timeout invalidates only the exact cached promise used by the failed request;
the next request invokes `start_sidecar` again. HTTP status failures, external
request cancellation, authentication failures, and response-contract parsing
failures preserve the cache and cannot cause a blind restart loop. Long-lived
SSE uses the separate `fetchEventSource` path and is not subject to the ordinary
request timeout.

Release SSH profile actions reserve three seconds of that renderer deadline for
HTTP and response handling. Credential resolution, trust-store load/probe and
confirmation, transport construction, and the trusted SSH connectivity check
share one 12-second monotonic deadline rather than receiving independent
timeouts. Connection ownership is generation-bound: replacing A with B first
persists A as disconnected, cancels A's obsolete local operation, and closes its
transport before B's synchronous credential or trust parsing can fail.
On macOS, the provider normalizes only Apple's fixed `/var` and `/tmp` system
aliases to their `/private/...` forms before opening the known-host and
workspace-import stores' secure ancestors. The known-host store verifies the
requested alias against its held descriptor during initialization and then
reopens only the canonical ancestor; the workspace-import store repeats the
alias-to-held-inode check on every parent-chain open. Arbitrary symlinked
ancestors remain invalid.
Unconfirmed host-key candidates are process-only review data and are never a
verified profile fingerprint. Connect, host-key accept, and disconnect return
the frozen operation ETag in the response header as well as the body.

Before `start_sidecar` reuses a managed process that is still alive, native code
repeats the authenticated and unauthenticated session probes using the retained
credential. A failed probe marks the old process cleanup-pending, performs the
bounded TERM/KILL group cleanup, removes the old endpoint and credential, and
continues through a fresh launch. It never returns the stale bootstrap context.

The release renderer startup owner uses stricter retry semantics than ordinary
request cache recovery. Initial mount, retry, React StrictMode supersession, and
renderer unmount invoke `stop_sidecar`; a subsequent provider bootstrap waits
for both the superseded bootstrap and native stop to settle before invoking
`start_sidecar`. Cancellation is issued immediately rather than queued behind an
in-flight start, allowing the native cancellation epoch and bounded join path to
reclaim a not-yet-published child. Therefore a release startup retry crosses a
real native lifecycle boundary and cannot adopt the failed attempt's credential.

Release policy does not read `OPENEVO_DESKTOP_SIDECAR_COMMAND`,
`OPENEVO_DESKTOP_SIDECAR_PROGRAM`,
`OPENEVO_DESKTOP_SIDECAR_ARGS_JSON`,
`OPENEVO_DESKTOP_SIDECAR_WORKDIR`, or
`OPENEVO_DESKTOP_BACKEND_BASE_URL`, and removes those variables from the child
environment before spawn. The `_PYI_*` removal and reset rule above is applied
in addition to this fixed product override list. It has no `sh -c`,
source-checkout Python, or
direct-backend fallback. Debug builds retain a development launcher behind
`cfg(debug_assertions)`: an optional program plus JSON string-array argv is
passed directly to `Command` without shell parsing, and the local host and port
arguments, inherited listener, and instance channel remain native-host owned.
Linux executes the verified anonymous file through `/proc/self/fd`. macOS keeps
the same digest-verified inode linked inside an owner-only `0700` launch
directory for the complete PyInstaller onefile parent/child process lifecycle,
passes its open descriptor to the FD-aware sidecar bootloader, and unlinks the
private pathname only after process-group cleanup is confirmed. This avoids
relying on macOS Mach-O execution through `/dev/fd` while preserving
descriptor-bound archive reads. The pathname is private native-host state and is
never renderer-visible, but it intentionally remains available for PyInstaller's
later child `execvp` until the owned process lifecycle has ended.
Debug-only override and source-launcher code is absent under production cfg;
the Desktop workflow compiles, lints, and tests both debug and release cfg.

### Workspace import store trust boundary

The Rust host holds the selected directory open with no-follow directory flags
for the complete archive handoff and revalidates the path against that descriptor's
device/inode identity. Only one OS picker may be physically active at a time. A
selection captures the current sidecar instance before opening the picker and is
rejected if that instance restarts before handoff. The renderer-visible Desktop
session credential cannot call the hidden import route; Rust uses a separate
per-instance native handoff credential delivered only through the inherited child
channel.

The private workspace import store uses a store authentication key that is a
separate raw 256-bit file in the root's parent directory. The key is not stored
in the root marker, archive xattr, or self-describing import metadata. A
domain-separated HMAC authenticates the durable parent/root binding marker and
each import's exact reference, ownership/idempotency fields, directory,
archive, and metadata inode identities, and archive generation token. Legacy
unauthenticated markers or
metadata are not adopted. A missing key for existing state, an invalid MAC, or
a replaced key that no longer authenticates the state fails closed.

Each process retains no-follow descriptors for the key, parent, and root and
checks their inode/path bindings at every locked operation. Ingest reopens and
verifies the actual archive and authenticated metadata after both files and the
directory have been fsynced, immediately before atomic no-replace publication,
and again after publication before returning the reference. It records a new
temporary directory's device/inode identity immediately after creation, and all
pre-publication rollback cleanup is conditional on that exact pathname binding;
replacement or unprovable state is retained for bounded startup reconciliation.
Resolve copies to
an inode-bound private snapshot, reopens and hashes that snapshot, unlinks the
verified pathname, then hashes the unlinked read-only descriptor again before
yielding it. These checks close the previously identified replacement and
equal-length rewrite windows at the store's verification boundaries.

Project persistence and private import storage are separate durable authorities.
After a successful source replacement or project deletion, the release provider
uses a fixed provider-reference-lock then import-root-lock order. It compares
`import_ref` identity rather than display metadata, rereads the complete durable
reference set under that guard after commit, and removes the previous exact import
only when its ID remains unreferenced. Cleanup retry does not change the committed
project result. On every sidecar start, bounded reconciliation is deferred until
the same guard yields the exact durable reference set. It first verifies every
referenced import and ownership without deleting anything; only a successful first
phase removes unreferenced picker snapshots. Missing or corrupt references preserve
the observed store and fail startup closed.

Picker snapshots have an explicit pending lease before they become project
authority. The lease is persisted as a keyed, one-way archive-xattr marker and
the raw token exists only in the hidden sidecar response and Rust host memory.
Rust caps that map at 64 entries and exposes only action-ID
`cancel`/`adopt`/`discard` commands to React. A create or patch adopts only after
its provider transaction is durable. Drawer close, reset, source replacement,
stale completion, and save failure all request discard. Discard takes the provider reference guard first,
rereads the full durable reference set, and then takes the import lock; a
concurrent commit therefore either retains/adopts the exact import or fails its
own pre-commit verification after an earlier discard. Startup adopts referenced
pending markers and removes unreferenced ones within the retained and pending
count/byte capacities.

Picker cancellation is a separate hidden action lifecycle. Rust generates a
per-action secret, releases the active claim immediately, and calls the private
cancel route; neither the secret nor route enters the renderer contract. Bounded
cancel-before-start tombstones close command-order races in both native host and
sidecar. The import HTTP reader polls cancellation and stops waiting within a
three-second grace period instead of retaining the 300-second import deadline.
Python checks the same operation identity during scan, archive I/O, hashing,
validation, and cancellable acquisition of both in-process and cross-process
store locks. The atomic no-replace publish is the linearization point. Before it,
cancellation quarantines/removes only the known temporary inode; after it, ingest
returns the recoverable lease so Rust can remember it before guarded discard.

This is not an OS isolation boundary against an arbitrary process running as
the same UID. Such a process can read the durable authentication key and can
write an already-open regular-file inode despite mode `0600`; no portable file
permission or HMAC construction can prevent that. During one sidecar process
lifetime, held descriptors provide replacement detection at operation
boundaries, not protection from a hostile same-UID writer between the final
check and consumption. Across an offline/restart boundary, replacement of the
root, marker, or xattr is detected only while the separate authentication key
remains uncompromised; an attacker that also reads/replaces that key can forge
the store. Stronger same-UID isolation requires a platform credential boundary
such as a separately entitled key service and is outside this store module.

The release native-folder bridge does not send a path to React or to any public
Local API operation. Rust records the selected directory's device and inode and
sends those values with the path only over the authenticated private loopback
route. The sidecar opens every absolute component with `O_NOFOLLOW`, rejects a
final identity mismatch, scans the complete tree twice around archive creation,
and checks every reopened entry against its first-scan identity. It accepts only
NFC UTF-8 regular files and directories within the frozen entry, path, depth,
file, extracted-byte, and archive-byte budgets. Symlinks and special files fail
closed. Directory names are charged to one global entry budget when enumerated,
before recursive processing; each `scandir` materializes at most the remaining
budget plus one before rejection and sorting. Non-empty regular files must have
POSIX 512-byte `st_blocks` allocation covering their logical size and expose a
complete no-hole extent through `SEEK_DATA`/`SEEK_HOLE`. This rejects a low-block
sparse file even when the filesystem returns the standard's minimal `0,size`
extent map. Unavailable or inconsistent allocation/extent evidence fails closed,
including allocated unwritten extents. Fully allocated ordinary APFS files are
accepted under minimal extent semantics; compressed, dataless, or other files
whose reported allocation is smaller than logical size are explicitly unsupported
because the bridge cannot prove them non-sparse.
The deterministic tar is an unlinked mode-`0600` temporary regular file before
`WorkspaceImportStore.ingest_pending` sees it.
The macOS workflow executes the focused scanner/store/lifecycle suite on APFS,
including a fully allocated ordinary file and an 8-MiB logical sparse file with
only one 4-KiB write; Linux continues to run the same portable suite and its
existing extent semantics.

The private action deterministically selects the opaque import ID, so an exact
retry with the same folder bytes converges and a changed body conflicts. A new
project ID derives from that opaque import ID; existing-project replacement
passes the saved project ID only on the private native boundary. Ownership is
then reproducible from the project/import owner, opaque import ID, and archive
digest for verification and later Core upload. This lets distinct projects own
identical archives without sharing operation/idempotency authority while exact
same-action replay converges across restart. The private route is excluded from
OpenAPI, uses the separate process-owned native handoff credential rather than
the renderer's Desktop session token, bounds the JSON request before parsing,
and returns only the frozen path-free `ProjectSourceV1` shape.

The child calls `setsid`, so its PID is also the ID of a new session and process
group. Before exec, it forks a minimal watchdog in that group. The native host
installs the writer of a close-on-exec parent-liveness channel in state before
calling `Command::spawn`; the watchdog retains only the reader. Writer EOF makes
the watchdog signal the group with TERM, wait 250 ms, and escalate to KILL.
The sidecar branch closes both liveness descriptors before exec, while inherited
FD 3 and FD 4 retain their listener and executable meanings. Thus cancellation,
normal host process exit, and a host crash after the watchdog exists cannot
leave an execing or running sidecar group detached from host liveness.

Startup uses one atomic epoch-and-phase word with `Idle`, `Reserved`,
`Spawning`, `Published`, and `Cancelled` states. The
`Reserved -> Spawning` compare-exchange is the spawn linearization point. If
cancellation advances first, that transition and `Command::spawn` cannot occur;
if spawning wins first, later cancellation closes the liveness writer and the
phase can no longer publish. There is no mutex launch gate. Before the spawn
transition, the manager publishes an exclusive `starting` slot containing the
listener, private directory, and executable FD, plus a state-owned handoff. The
handoff outcome lock is independent of cancellation and is held while
`Command::spawn` waits, so a returned `Child` is installed in the handoff before
the startup thread can wait for the manager lock. Short or poisoned manager-lock
contention therefore cannot drop an unpublished child. Once transferred,
readiness takes only bounded, short manager locks; credential I/O, network
polling, and executable validation do not hold them. Running publication also
uses bounded lock acquisition, so ordinary status contention does not
spuriously reject an already-ready process.

Group signaling is generation-bound to the unreaped leader. Status, restart,
and cleanup observe leader exit with `waitid(..., WNOWAIT)` and do not reap it.
Immediately before every TERM or KILL, cleanup repeats that non-reaping child
check; an inspection error authorizes no numeric-PGID signal. While the manager
is `Anchored`, the retained child identity prevents PID/PGID reuse and authorizes
TERM/KILL to the group. Linux enumerates `/proc`; a visible PID's denied or
malformed `stat` data fails closed, while only a real `NotFound` race is skipped.
macOS treats both return values from `proc_listpgrppids` as PID counts. Only the
buffer call receives byte capacity. A full buffer causes bounded growth and
retry. Native code clears `errno` immediately before every count and buffer call;
a zero return is an empty result only when the captured `errno` remains zero.
Zero with nonzero `errno`, impossible sizes, over-counts, or persistent
truncation fail closed.
Both platforms therefore retain ownership when the leader has exited but a
descendant remains in the process group.
Darwin can return `EPERM` when group signaling finds only unsignalable zombie
members. Only `EPERM` returned by the signal operation receives the typed
inconclusive outcome; an identical error from the preceding leader inspection
remains a hard inspection failure. Cleanup may proceed only if the subsequent
non-reaping leader check and process-group enumeration independently prove that
the leader exited and no non-leader member remains. Any other signal error,
absent proof, or inspection error retains ownership.
Only after the leader has exited and the rest of the group is absent does cleanup
switch irreversibly to `Finalizing` and call `Child::try_wait`. A final reap
error retains ownership for retry, but every retry in `Finalizing` is reap-only:
it can never signal the old numeric PGID. This avoids stale-PGID signaling even
when a reap operation has consumed the leader before reporting failure.

Publication starts exactly one detached native monitor for the random instance
identity stored in the manager slot. It uses `waitid(..., WNOWAIT)` to detect a
post-readiness leader exit without surrendering the PID/PGID anchor. Under the
same manager lock used by stop and restart, it terminates and waits for residual
group members (including the parent-liveness watchdog), enters `Finalizing`,
reaps the leader, closes the watchdog writer, and removes the matching slot.
Cleanup failure retains the exact slot as `cleanup_pending` for monitor or
explicit-stop retry. A monitor for an older random instance exits when it sees a
replacement slot, so it cannot signal or reap a reused numeric PID/PGID; the
shared manager lock also prevents stop and monitor from double-reaping.

Every post-spawn failure therefore leaves the process either in the handoff or
in the manager slot until bounded group cleanup succeeds. Pending and failed
handoffs are resolved without a child; spawned handoffs can be cleaned directly
if manager transfer cannot complete. Mutex poison is recovered as fail-closed
retained state, cleanup failure changes the manager to `cleanup_pending`, and a
lock timeout leaves ownership unchanged. Restart remains blocked, while explicit
stop can retry. No failure path uses `mem::forget`, leaks a `Child`, performs an
unbounded `Child::wait`, or drops a live manager-owned process as cleanup.
When cancellation has already advanced, a child that exits before birth-identity
inspection still reports typed startup cancellation after cleanup succeeds;
cleanup failure continues to take precedence and retain retryable ownership.

Stop and exit advance cancellation with an atomic compare-exchange before any
bounded mutex access; neither waits for `Command::spawn` or a launch mutex. They
then try to close the parent-liveness writer and acquire handoff/manager state
within configured lock budgets. Explicit stop may return
`sidecar_state_timeout` while retained startup ownership is still contended;
the watchdog still terminates an already-created child after writer closure,
and a later stop retries reap and state cleanup. The exit hook also sets the
shutdown flag first; actual process exit closes any writer that bounded cleanup
could not acquire. TERM/KILL polling remains fixed at one second per phase for
normal manager cleanup.

No claim is made that the thread inside `Command::spawn` has an independent
wall-clock bound before the watchdog has been forked. The pre-exec path is kept
to async-signal-safe libc work: `setsid`, `fork`, `close`, `dup2`, `fcntl`,
`fstat`, `sigaction`, `read`, `kill`, `nanosleep`, `_exit`, and fixed-size
comparisons. It takes no application lock, allocates no heap memory, and emits
no log. Once the watchdog exists, closing the liveness writer terminates a child
blocked in a later pre-exec hook or exec/error-channel handoff without making
stop or exit wait on that channel.

Sidecar stdout and stderr are connected to the null device. There is no native
raw-log buffer and no `app_logs` Tauri command, so renderer JavaScript cannot
receive child output; sidecar status also omits command, path, argv, credential,
and backend details.

The native host and packaged Python launcher share the four-key frame and strict
Local API v1 inventory described above. `/health` is provided by the release
provider and proves the frame instance with the challenge HMAC. `/version`
reports the frozen Local API digest and release provider identity. Its
`source_commit` is a 7-40 character lowercase hexadecimal commit generated from
`git rev-parse --verify HEAD^{commit}` by `build_sidecar.py`, stored in a closed
build metadata file, and embedded through PyInstaller `--add-data`; runtime code
does not infer it from environment variables or Git, and release startup rejects
an all-zero placeholder. Development and test apps must inject their source
commit and non-release channel explicitly. There is no direct-backend fallback.

The release sidecar now owns the initial SSH lifecycle behind the frozen Local
API profile routes. A connect action atomically validates its idempotency
envelope and profile ETag, reserves live idempotency capacity plus fixed-size
terminal slots for both persisted response copies, publishes the profile as the
current `connecting` owner, and creates a running local operation before
external work. A process-wide action lock spans that entire sequence, including
the SSH call and terminal publication, for all profiles and idempotency keys; the
provider reservation order therefore cannot diverge from lifecycle invocation
order. The sidecar publishes a state invalidation after that reservation and
another after terminal publication, so the renderer can observe and cancel the
running operation while the original connect request is blocked. The action
then either loads an exact profile/host/port/fingerprint trust
binding or performs a non-mutating `ssh-keyscan`. A new key is returned only as
an explicit `host_key_review` state. Acceptance repeats the probe, requires the
same algorithm and fingerprint, publishes the private known-host binding, and
runs a bounded SSH connectivity check. Success, failure, and crash cancellation
update the reserved profile, operation, and idempotency response in one
transaction, so concurrent capacity consumption and the request's now-stale
ETag cannot break finalization.
The lifecycle registers a newly constructed transport as a generation-bound
candidate before the connectivity command starts. `cancelLocalOperation` or
disconnect removes that candidate under the lifecycle lock, then closes it
outside the lock; the close interrupts owned subprocesses and causes the
superseded connect call to exit without publishing a late connected state.
Terminal cancel replay does not close the lifecycle a second time.
Disconnect reservations are non-displacing and do not publish `connecting`; the
sidecar checks the process lifecycle owner before invoking disconnect, so a
request for profile B cannot rewrite profile A or close A's transport. A
pre-commit completion error leaves the nonterminal reservation and its terminal
capacity intact until the same finalizer transaction publishes failure. A
commit-return error instead observes the persisted terminal first: committed
success remains success and keeps its transport, even if concurrent CRUD fills
the now-released budget. Failure finalization uses the same exact reservation,
request digest, profile, and operation identity for a read-only observation after
any commit-return error. An observed terminal is authoritative; an observed
`running` state is retried once and is never inferred to have committed. After a
terminal failure is durable, transport cleanup runs only when the current durable
profile remains disconnected and the process lifecycle still names that same
profile as owner. Exact failed replay repeats that owner-checked cleanup, repairing
a prior cleanup interruption without repeating SSH work or disconnecting a newer
owner. Every persisted terminal body and ETag is permanently frozen; late
complete/fail calls return it unchanged and only close a transport still owned by
their own stale result. The failed operation embeds the exact API error used by
later replays. Profile deletion atomically rejects any queued, running, or
cancelling profile operation, including a disconnect reservation that deliberately
leaves an already-disconnected profile state unchanged. Once that operation is
terminal and its owned transport is closed, its historical record no longer
blocks deletion. Process restart resets persisted runtime connection state to
disconnected and does not claim a surviving tunnel. It only reconciles
nonterminal reservations, writing
their cancelled operation and idempotency response together. SSH success alone
reports `core_not_started`, not an online Core.

The exhibition candidate does not ship a native SSH credential broker. Desktop
release profile creation and patch accept only `ssh_agent`; historical
`native_password` and `native_private_key` values remain parseable as reserved
contract values but cannot connect; the user must explicitly save the profile as
SSH agent before it can connect.
Startup clears historical credential-slot status. There is no Tauri password
prompt, private-key picker, Keychain registry, sidecar credential vault, askpass
helper, `ssh-agent` child, `ssh-add` child, or native credential handoff route in
the packaged composition. Linux and macOS therefore share the same fail-closed
boundary. Authenticated proxy slots and the self-deployed Hugging Face token slot
are likewise reserved and unavailable; HTTP(S) proxy URLs without user-info
remain supported.

Release SSH, ssh-keyscan, and rsync calls use platform-fixed allowlisted absolute
binaries. The Desktop sidecar deployment transport verifies root ownership,
regular-file type, executable mode, link count one, non-group/world-writable
ancestors and file metadata. Linux launches through the held executable FD. On
macOS, the top-level SSH/rsync birth child compares that FD with the fixed system
path immediately before execution; ssh-keyscan is verified before launch and
again after completion. The original host
`SSH_AUTH_SOCK` path is never supplied to OpenSSH. Every SSH/rsync/tunnel spawn
first connects and revalidates the held upstream socket, then exposes only a
fresh owner-private one-shot relay. Kernel peer PID, the owned child session and
process group, and the held SSH executable vnode jointly authorize its sole
downstream connection. Rsync names the held SSH FD in `-e` on Linux; on macOS it
uses the fixed root-owned `/usr/bin/ssh` path while inheriting the verified FD as
the parent-side identity authority. Concurrent privileged replacement of
`/usr/bin` is outside the unsigned Desktop threat boundary. Relay buffers,
accept lifetime, cleanup retries, and
retained cleanup authorities are bounded; uncertain path cleanup never removes
a replacement. Process-group birth and cleanup authority is retained across
cancellation. Linux/macOS externalBin combination smokes cover this native
launch boundary; candidate-only app/DMG launch and downloaded-asset roundtrip
remain separate gates. ACL contract tests cover inherited/mutating entries,
unknown tags/permissions, post-initialization mutation, and cleanup replacement
windows.

Before that outer lifecycle harness, the workflow installs the exact remote Core
wheel in a clean Python environment and passes the exported build-generated
framework lock, rather than synthesizing a new lock at smoke time, to both
framework and remote-capability smokes. The latter starts the real
Core supervisor API with that lock and checks bearer protection,
both release profiles, registry identity, and target-rooted methods over HTTP.
It separately starts a Linux packaged sidecar fixture built from the same commit
through the same inherited-listener
and native instance frame used by the Rust host, verifies the native
readiness proof, frozen release digest and features, strict Desktop state,
Desktop session protection, and packaged assets, and rejects
the removed legacy `/openevo-api/desktop/capabilities` route. End-to-end remote
profile, SSH tunnel, and active-project capability forwarding remain a distinct
release-composition gate; these two process smokes do not claim to replace it.
The supervised Core process smoke runs only in the Linux release-smoke job,
because the remote Core service intentionally depends on Linux pidfd semantics;
the macOS candidate job validates the exact wheel/lock pair and launchers but
does not pretend to host the remote Core locally. The Linux fixture is not a
macOS candidate artifact. Remote-smoke cleanup is conditional on the exact
attachment generation and release identity still matching under the Core
supervisor locks, so a replacement service cannot be stopped by stale cleanup.

## External Beta Artifacts

The implemented release workflow must produce, from one reviewed `stable`
commit:

- `OpenEvo-Desktop-<version>-<aarch64|x64>.dmg` for the architecture actually
  built by the current runner; no universal claim is made;
- the DMG SHA256 checksum;
- the exact Core install artifact and checksum;
- `core-install-artifact.json` matching the Core bytes bundled with or fetched
  by Desktop;
- `release-candidate.json` and canonical `SHA256SUMS` binding the DMG, Core
  wheel, framework lock, Core descriptor, source commit, actual architecture,
  raw/DMG-copy Mach-O evidence, native app smokes, and supply-chain evidence;
- `python-requirements.txt`, exported without the OpenEvo project itself and
  digest-bound to the exact third-party `pip-audit` report summary;
- release notes and the dependency/security evidence required by the canonical
  spec.

PyPI is not part of the External Beta release.

The changed closed contracts use version 2 for dependency/security summaries,
native smoke evidence, the Core descriptor, and the release candidate manifest.
The unchanged license inventory and framework lock keep their existing versions.

## Build Inputs

The release build must use locked and reviewed inputs:

- `npm ci` and `desktop/package-lock.json`;
- Rust stable with `desktop/src-tauri/Cargo.lock` and `cargo --locked`;
- the supported Python version and sidecar build dependencies resolved by
  `uv sync --frozen` from `uv.lock`; this includes `build`, `setuptools`, and
  `wheel`, and the Core wheel build disables build isolation so it cannot resolve
  an unreviewed build environment. The FD-aware bootloader build additionally
  fetches the exact PyInstaller sdist URL recorded in that lock and rejects a
  size, SHA-256, extraction-budget, path, or audited-source mismatch;
- `desktop/src-tauri/tauri.conf.json`;
- generated sidecar binary
  `desktop/src-tauri/binaries/openevo-desktop-sidecar-$TARGET_TRIPLE`;
- the exact Core artifact descriptor and bytes selected for the candidate.

Developer fallbacks such as source-checkout Python launchers, custom sidecar
commands, backend URL overrides, and dry-run transports must be rejected by a
release build.
The repository does not carry a shell-script sidecar fallback; the generated
target-triple binary is a required build input.

The sidecar builder never removes repository-global `build/` or existing
`src/openevo/wheels`. A caller-selected Core wheel output must not exist. The
Core wheel is built in a private temporary tree, and only the verified exact
wheel/lock pair is published through a private sibling directory and atomic
no-replace rename. Existing output, symlinks, and generated-path overlap fail
before publication; stale random staging is non-authoritative and is never
automatically recovered or removed. `--no-clean` only preserves the
sidecar-owned `sidecar-dist` and `sidecar-build` paths, matching the existing
clean/no-clean contract. The final target-triple externalBin is published from a
verified sibling staging file with atomic replacement, so a failed replacement
does not remove or partially overwrite the previously built target.

## Release Build Sequence

The replacement workflow must:

1. run Python, frontend, sidecar, Rust, identity, and package-inventory tests;
2. build and clean-install the exact Core artifact;
3. create and validate its framework lock and SHA256 through the authoritative
   Core lock model/loader; the sidecar build must embed exactly that wheel and
   lock without a pre-staged artifact;
4. build the sidecar, Vite assets, Tauri app bundle, and DMG on a supported
   macOS runner;
5. mount the DMG, copy the app into a clean location, and launch that copied
   application with a clean user profile; before each launch, inspect the Tauri
   executable and sidecar with `file -b` and `lipo -archs`, then require the raw
   and copied observations to match the declared candidate architecture;
6. exercise first-run through a descriptor-matched remote Core health check;
7. use the authenticated paginated release inventory to require the candidate
   release, including a private draft, and real Git tag to be absent; create a
   draft GitHub prerelease with a per-attempt random ownership marker; upload all
   required assets, download them into a clean directory, and revalidate names,
   versions, commits, checksums, title, tag name, target commit, body, draft
   state, prerelease state, ownership, and immutable numeric release ID at a
   discrete API read; persist cleanup authority once in an owner-only file;
8. retain the point-in-time draft verification record as a run-attempt-qualified
   Actions artifact, prove no real Git tag was created, and leave the candidate
   as an unpublished review draft.

Final publication remains disabled. The manual candidate workflow implements
the packaging-level draft roundtrip. If the job fails or is cancelled before
its final verification marker, cleanup first verifies the exact draft metadata
and random ownership marker, then retries deletion by that draft's immutable
numeric release ID rather than resolving the mutable tag again. Cleanup
never deletes a Git tag and fails unless a same-name tag is absent. It
deliberately leaves
a successful candidate as an unsigned draft prerelease; a maintainer cannot use
that result to bypass the still-pending science, benchmark, privacy, review,
signing, or notarization gates.

The GitHub draft is administratively mutable; it is not an immutable publication
authority. Validation describes individual GitHub API reads, not an atomic
snapshot at workflow completion. Any post-workflow edit, asset replacement, or
tag movement invalidates the candidate and requires deleting it and running a
new candidate.

## Packaged Runtime Rules

- Tauri owns sidecar lifecycle, native state, private launch preparation, local
  listener allocation, and bounded process-group shutdown/recovery. User-secret
  Keychain resolution and native credential commands are not packaged.
- The sidecar binds locally, opens the SSH/Core connection, and forwards typed
  operations. It does not execute science runs after Core is healthy.
- Packaged resources contain no credentials, benchmark automation, source
  checkout dependency, development override, or stale web bundle.
- The unsigned/not-notarized warning and manual Gatekeeper launch procedure
  must match behavior observed from the copied app.

The current packaging-only candidate launches a copied app without simulating
browser-download quarantine. It therefore records the Privacy & Security allow
flow as pending and does not satisfy that final Gatekeeper requirement.

## Blocking Validation

Release evidence must cover:

- supported architecture and minimum macOS version;
- exact Mach-O slices for both the Tauri executable and sidecar in the raw app
  and the app copied from the mounted DMG;
- Core Python/platform compatibility and a Linux verifier selected from the
  descriptor's closed supported-platform list;
- sidecar start, crash recovery, tunnel loss, quit, and relaunch;
- first-run bootstrap against the exact Core descriptor;
- the ordinary science flow without CLI use;
- no-default-telemetry and secret-canary checks;
- mounted/copy launch and supported window-size/accessibility checks;
- artifact inventory, SHA256, source commit, and release-note consistency;
- third-party-only Python export/report equality and vulnerability audits with
  no ignored advisories; the Rust audit tool must parse the current RustSec
  database or the evidence collector fails closed.

Local source checks remain useful during implementation:

```bash
source .venv/bin/activate
cd desktop
npm ci
npm test -- --run
npm run typecheck
npm run build:openevo
npm run build:sidecar
cd src-tauri
cargo metadata --locked --format-version 1
cargo fmt --check
cargo check --locked --all-targets
cargo check --locked --release --all-targets
cargo clippy --locked --all-targets -- -D warnings
cargo clippy --locked --release --all-targets -- -D warnings
cargo test --locked
cargo test --locked --release
OPENEVO_PACKAGED_SIDECAR_PATH="$PWD/binaries/openevo-desktop-sidecar-$(rustc --print host-tuple)" \
  cargo test --locked tests::packaged_external_bin_native_launch_smoke -- --ignored --exact
```

They do not replace the packaged-DMG and downloaded-draft validation gates.
