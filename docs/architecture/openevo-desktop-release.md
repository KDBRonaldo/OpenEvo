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
- a Linux release smoke that builds and launches the actual packaged sidecar;
- a disabled `.github/workflows/openevo-release-artifact.yml` placeholder that
  publishes nothing.

This scaffolding is not DMG release evidence. The executable sidecar smoke proves
its local health/static-asset path and discovery of the embedded Core wheel plus
framework lock, but does not prove that a mounted and copied macOS app starts or
completes the science workflow.

The release workflow's outer smoke installs Core and exercises it through the
Desktop harness imported from the source checkout. Its workflow and script names
must not be interpreted as packaged Desktop evidence. The outer smoke wheel is
assembled from a separate temporary source tree, so staging its embedded Core
wheel does not alter `src/openevo/wheels` in the checkout.

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
  an unreviewed build environment;
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

- Tauri owns sidecar lifecycle, native state, Keychain references, app logs,
  file selection, and clean shutdown/recovery.
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
npm run build:openevo
npm run build:sidecar
cd src-tauri
cargo metadata --locked --format-version 1
cargo test --locked
```

They do not replace the packaged-DMG and downloaded-draft validation gates.
