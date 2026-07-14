# OpenEvo Desktop Release Packaging

> Pre-release status: the current repository contains Tauri packaging
> scaffolding, but the External Beta release workflow is intentionally disabled.
> No current workflow builds, uploads, or publishes a release-ready DMG.

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
  name/version metadata, and passes that exact file to PyInstaller with
  `--add-data` at `openevo/wheels`. Archive inspection requires one matching
  member whose byte SHA-256 equals the built and optionally exported wheel;
- source-level frontend, sidecar, Rust, and package-inventory tests;
- Linux and macOS CI jobs that build the actual PyInstaller externalBin and
  exercise it through the production Rust native-launch path;
- a disabled `.github/workflows/openevo-release-artifact.yml` placeholder that
  publishes nothing.

This scaffolding is not DMG release evidence. The executable sidecar smoke proves
its local health/static-asset path, discovery of the embedded Core wheel plus
framework lock, and its token-protected capability proxy against a real backend.
It does not prove that a mounted and copied macOS app starts or completes the
science workflow.

The release workflow's outer smoke installs Core and exercises it through the
Desktop harness imported from the source checkout. Its workflow and script names
must not be interpreted as packaged Desktop evidence. The outer smoke wheel is
assembled from a separate temporary source tree, so staging its embedded Core
wheel does not alter `src/openevo/wheels` in the checkout.

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

On macOS, mode and owner checks are not the complete write policy. Native code
reads the extended ACL from every held component FD and from the held sidecar
source FD with `acl_get_fd_np`. A NULL ACL result, malformed entry, unknown tag,
unknown ALLOW permission, or ALLOW entry containing write-data, append, delete,
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
`instance_id`, `readiness_key`, and `session_token`; protocol is
`openevo-native-sidecar-v1`, instance ID is 128 fresh bits, and both credentials
are independently generated 256-bit values. Duplicate, missing, unknown,
malformed, or trailing input is rejected by the strict sidecar integration.
The packaged Python launcher applies the same 512-byte bound, requires the
closed four-key object and lowercase fixed-width hex values, and passes all
three native values directly to `create_release_desktop_local_api_app`. It
mounts only that release Local API and the audited product web. It does not
construct the legacy sidecar app, expose `/openevo-api/*`, translate the Desktop
session header into a legacy mutation token, or accept a backend base URL. Its
durable provider state is isolated under `<Desktop config root>/local-api-v1`.
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

Before `start_sidecar` reuses a managed process that is still alive, native code
repeats the authenticated and unauthenticated session probes using the retained
credential. A failed probe marks the old process cleanup-pending, performs the
bounded TERM/KILL group cleanup, removes the old endpoint and credential, and
continues through a fresh launch. It never returns the stale bootstrap context.

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
directory only long enough for `exec`, passes its open descriptor to the
FD-aware sidecar bootloader, and unlinks the private pathname immediately after
the child is created. This avoids relying on macOS Mach-O execution through
`/dev/fd` while preserving descriptor-bound archive reads and preventing a
renderer-visible or reusable launch path.
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
removes the previous exact import without changing the already-committed project
result if cleanup must be retried. On every sidecar start, a bounded reconciliation
derives the complete retained set from durable project rows, verifies each exact
reference and ownership, removes unreferenced picker snapshots, and fails closed
when a referenced import is missing or corrupt.

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
closed. The deterministic tar is an unlinked mode-`0600` temporary regular file
before `WorkspaceImportStore.ingest` sees it.

The private action deterministically selects the opaque import ID, so an exact
retry with the same folder bytes converges and a changed body conflicts. A new
project ID derives from that opaque import ID; existing-project replacement
passes the saved project ID only on the private native boundary. Ownership is
then reproducible from project ID and archive digest for verification and later
Core upload. The private route is excluded from OpenAPI, uses the process-owned
Desktop session token, bounds the JSON request before parsing, and returns only
the frozen path-free `ProjectSourceV1` shape.

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

This phase does not implement a macOS Keychain secret broker. In particular,
`password_ref` and `passphrase_ref` cannot yet be resolved for SSH operations,
and the native command surface exposes no placeholder Keychain operation. These
native-host changes and Linux/macOS externalBin combination smokes do not prove
code signing, notarization, closure of the same-UID pathname TOCTOU described
above, mounted/copied macOS application launch, first-run
remote bootstrap, or downloaded artifact identity, and do not make the DMG
release-ready. ACL permission-mask policy tests run cross-platform and a macOS
cfg test exercises `acl_get_fd_np` on a fresh ACL-free anchored file. No test
currently creates a real writable extended-ACL fixture on macOS, so rejection
of an installed mutating ACE remains a macOS-runner fixture gap rather than
claimed release evidence.

Before that outer lifecycle harness, the workflow installs the exact remote Core
wheel in a clean Python environment and runs
`scripts/ci/smoke_openevo_remote_capabilities.py` with the PyInstaller sidecar
path. That smoke starts the real `openevo-backend serve --framework-lock`
process, starts the packaged sidecar on a real listener, and checks mutation-token
protection, both release profiles, registry identity, and target-rooted methods
over HTTP. A source `TestClient` or local capability fixture in the outer
lifecycle harness is not evidence for this packaged proxy path.

## External Beta Artifacts

The implemented release workflow must produce, from one reviewed `stable`
commit:

- `OpenEvo-Desktop-<version>-<aarch64|x64|universal>.dmg`;
- the DMG SHA256 checksum;
- the exact Core install artifact and checksum;
- `core-install-artifact.json` matching the Core bytes bundled with or fetched
  by Desktop;
- release notes and the dependency/security evidence required by the canonical
  spec.

PyPI is not part of the External Beta release.

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

The sidecar builder never removes repository-global `build/`, existing
`src/openevo/wheels`, or prior files in a caller-selected Core wheel output
directory. A selected output directory must be empty before the build and the
wheel is created there exclusively; non-empty or path-overlapping output fails
closed. `--no-clean` only preserves the sidecar-owned `sidecar-dist` and
`sidecar-build` paths, matching the existing clean/no-clean contract.

## Release Build Sequence

The replacement workflow must:

1. run Python, frontend, sidecar, Rust, identity, and package-inventory tests;
2. build and clean-install the exact Core artifact;
3. create and validate its descriptor and SHA256; the sidecar build must embed
   those exact Core bytes without a pre-staged wheel;
4. build the sidecar, Vite assets, Tauri app bundle, and DMG on a supported
   macOS runner;
5. mount the DMG, copy the app into a clean location, and launch that copied
   application with a clean user profile;
6. exercise first-run through a descriptor-matched remote Core health check;
7. create a draft GitHub Release, upload all required assets, download them into
   a clean directory, and revalidate names, versions, commits, and checksums;
8. publish the already-validated draft without rebuilding any bytes.

The workflow remains disabled until these steps and their failure paths are
implemented.

## Packaged Runtime Rules

- Tauri owns sidecar lifecycle, native state, private launch preparation, local
  listener allocation, and bounded process-group shutdown/recovery. Keychain
  resolution and renderer-visible native diagnostics are not implemented in
  phase one.
- The sidecar binds locally, opens the SSH/Core connection, and forwards typed
  operations. It does not execute science runs after Core is healthy.
- Packaged resources contain no credentials, benchmark automation, source
  checkout dependency, development override, or stale web bundle.
- The unsigned/not-notarized warning and manual Gatekeeper launch procedure
  must match behavior observed from the copied app.

## Blocking Validation

Release evidence must cover:

- supported architecture and minimum macOS version;
- sidecar start, crash recovery, tunnel loss, quit, and relaunch;
- first-run bootstrap against the exact Core descriptor;
- the ordinary science flow without CLI use;
- no-default-telemetry and secret-canary checks;
- mounted/copy launch and supported window-size/accessibility checks;
- artifact inventory, SHA256, source commit, and release-note consistency.

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
