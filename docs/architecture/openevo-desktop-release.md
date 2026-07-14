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
be owned by root or the effective user. The sidecar source owner must exactly
match the current executable owner, and that app owner must itself be root or
the effective user. This supports both a root-owned `/Applications` install and
a user-owned copied app while rejecting a third UID.

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

Execution uses inherited FD 4, never the source or private-copy pathname:
Linux uses `/proc/self/fd/4` and macOS uses `/dev/fd/4`. The sidecar builder
downloads only the exact size/SHA-256 PyInstaller sdist recorded in `uv.lock`,
applies an exact-source bootloader patch, and rebuilds it. When the native host
forces the non-secret `OPENEVO_NATIVE_EXECUTABLE_FD=4` setting, every PyInstaller
parent/child archive reopen uses that same inherited FD; the entry point removes
the marker before launcher `main` runs. The parent validates private FD
identity and digest before and after spawn, and the child validates its inherited
FD identity immediately before exec. Replacing any final pathname cannot alter
the launched bytes. Typed failures contain a stable code and user-readable
message without either host path.

The native host binds the loopback listener before spawn and transfers that
already-bound socket on inherited FD 3, removing the release-and-rebind port
window. Native code sends exactly one UTF-8 JSON frame of at most 256 bytes over
the child's stdin and then closes the pipe. Its exact keys are `protocol`,
`instance_id`, and `readiness_key`; protocol is
`openevo-native-sidecar-v1`, instance ID is 128 fresh bits, and readiness key is
256 fresh bits. Duplicate, missing, unknown, malformed, or trailing input is
rejected. Neither value is placed in argv, environment, a file, or native
status. The readiness key is never returned to the renderer; the non-secret
instance ID is returned only as part of the challenge-bound health response.

Readiness sends a fresh 256-bit challenge to `/health`. The closed Rust response
model requires the exact protocol and instance ID and verifies HMAC-SHA256 over
`protocol NUL instance_id NUL challenge`. Unknown fields, stale challenge
proofs, arbitrary HTTP 200 responses, invalid UTF-8, and responses over the
fixed byte limit cannot satisfy startup readiness.

Release policy does not read `OPENEVO_DESKTOP_SIDECAR_COMMAND`,
`OPENEVO_DESKTOP_SIDECAR_PROGRAM`,
`OPENEVO_DESKTOP_SIDECAR_ARGS_JSON`,
`OPENEVO_DESKTOP_SIDECAR_WORKDIR`, or
`OPENEVO_DESKTOP_BACKEND_BASE_URL`, and removes those variables from the child
environment before spawn. It has no `sh -c`, source-checkout Python, or
direct-backend fallback. Debug builds retain a development launcher behind
`cfg(debug_assertions)`: an optional program plus JSON string-array argv is
passed directly to `Command` without shell parsing, and the local host and port
arguments, inherited listener, and instance channel remain native-host owned.
Debug-only override and source-launcher code is absent under production cfg;
the Desktop workflow compiles, lints, and tests both debug and release cfg.

### Workspace import store trust boundary

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
and again after publication before returning the reference. Resolve copies to
an inode-bound private snapshot, reopens and hashes that snapshot, unlinks the
verified pathname, then hashes the unlinked read-only descriptor again before
yielding it. These checks close the previously identified replacement and
equal-length rewrite windows at the store's verification boundaries.

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

The child starts in its own process group. Explicit stop, startup failure,
restart cleanup, and Tauri `ExitRequested`/`Exit` handling signal the complete
group with TERM, poll for a fixed interval, escalate to KILL, and poll for a
second fixed interval. Stop and exit first advance a cancellation epoch so an
in-progress startup cannot publish or spawn after cancellation, and state-lock
acquisition is also time-bounded. No failure path performs an unbounded
`Child::wait`. An unexpected TERM, KILL, child-wait, or group-inspection failure
moves the manager to `cleanup_pending`; it retains `Child`, process-group ID,
private directory, executable FD, and listener, blocks another start, and lets
explicit stop retry cleanup. Resources are dropped only after the child is
reaped and the full process group is confirmed absent.

The exit hook first updates atomically shared cancellation and process-group
state, so it can issue TERM and KILL even while the manager mutex is busy. It
then retries manager-owned cleanup with bounded lock access. The configured
worst case is 250 ms emergency TERM grace, 3 seconds for state-lock access, and
two 1-second cleanup polls: 5.25 seconds plus constant-time syscalls.
Sidecar stdout and stderr are connected to the null device. There is no native
raw-log buffer and no `app_logs` Tauri command, so renderer JavaScript cannot
receive child output; sidecar status also omits command, path, argv, credential,
and backend details.

This phase does not implement a macOS Keychain secret broker. In particular,
`password_ref` and `passphrase_ref` cannot yet be resolved for SSH operations,
and the native command surface exposes no placeholder Keychain operation. These
native-host changes and Linux/macOS externalBin combination smokes do not prove
code signing, notarization, mounted/copied macOS application launch, first-run
remote bootstrap, or downloaded artifact identity, and do not make the DMG
release-ready.

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
