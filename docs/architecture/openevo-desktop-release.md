# OpenEvo Desktop Release Packaging

## What Changed

OpenEvo Desktop has a top-level `desktop/` product package for the
ordinary-user macOS release artifact. The Tauri native host owns the application
window, local sidecar process lifecycle, local port allocation, host logs, and
the command surface used by the React UI. The release artifact workflow produces
two artifact classes:

- `openevo-wheel`: the exact `openevo-<version>-*.whl` used for remote install.
- `openevo-desktop-dmg`: a macOS `.dmg` built from the Tauri desktop shell.

The `.dmg` is the ordinary-user release format for macOS. The browser-served
Vite app remains a development and CI smoke path, not the user-facing product
surface.

## Why

The wheel is still the installable Python/OpenEvo runtime artifact, but it is
not an ordinary desktop application package. Tauri provides the native desktop
host around the Vite-built Desktop UI so macOS users can install and launch
OpenEvo Desktop as an application bundle distributed in a `.dmg`.

## Consumers

- Release automation consumes `desktop/package.json` scripts,
  `desktop/src-tauri/tauri.conf.json`, and the Rust host under
  `desktop/src-tauri/src/main.rs` to build the `.dmg`.
- Release validators consume the complete artifact list and require both the
  exact OpenEvo wheel and at least one `.dmg`.
- Ordinary macOS users consume the `.dmg`.
- Developers and CI smoke tests consume the Vite development server and packaged
  `desktop/packaging/web` assets to validate the same Desktop UI without
  treating that browser path as the released product.

## Input Contract

The desktop packaging path expects:

- Node dependencies installed with `npm ci` in `desktop/`.
- Rust stable toolchain available on the macOS runner.
- Rust dependencies locked by `desktop/src-tauri/Cargo.lock`; CI runs
  `cargo metadata --locked --format-version 1` before packaging.
- Vite output produced by `npm run build:openevo`.
- A bundled sidecar binary produced by
  `python desktop/packaging/build_sidecar.py`. The script uses PyInstaller to
  package `desktop.server.launcher` and writes
  `desktop/src-tauri/binaries/openevo-desktop-sidecar-$TARGET_TRIPLE`.
- Tauri config at `desktop/src-tauri/tauri.conf.json` with:
  - `productName`: `OpenEvo Desktop`
  - `identifier`: `org.openevo.desktop`
  - `frontendDist`: `../dist`
  - bundle target: `dmg`
  - `externalBin`: `binaries/openevo-desktop-sidecar`
  - macOS `minimumSystemVersion`: `12.0`
- Tauri icon assets under `desktop/src-tauri/icons/`.
- Rust host commands for sidecar status, sidecar start/stop, SSH tunnel port
  reservation, keychain references, and app logs.
- The installed app starts the bundled `openevo-desktop-sidecar` binary from
  the Tauri resource path. In source/development environments where that binary
  is not present, the host falls back to:

```text
python3 -m desktop.server.launcher --host 127.0.0.1 --port <allocated-port>
```

The command can be overridden with `OPENEVO_DESKTOP_SIDECAR_COMMAND`; `{port}`
in the override is replaced with the allocated local port.
`OPENEVO_DESKTOP_SIDECAR_WORKDIR` can set the launcher working directory.

## Output Contract

`npm run build:desktop` runs `tauri build`; Tauri first executes
`npm run build:openevo`, and the macOS workflow first builds the
`openevo-desktop-sidecar-$TARGET_TRIPLE` external binary. The `.dmg` embeds the
ordinary-user OpenEvo-only Vite build and the local sidecar binary rather than
the shared dashboard shell or a source-checkout dependency. On GitHub Actions
macOS runners, the release workflow uploads:

```text
desktop/src-tauri/target/release/bundle/dmg/*.dmg
```

The release artifact validator accepts wheel paths for wheel-content checks and
an optional complete artifact list for release-list checks. The complete list
must include:

- `openevo-<version>-*.whl`
- at least one `*.dmg`

Wheel validation remains scoped to `.whl` files only.

## Current Boundaries

- The native host starts and stops a bundled local sidecar process and waits for
  `/health` before routing frontend API calls to it.
- SSH tunnel support currently reserves a local port and exposes the native
  command boundary. Establishing a full authenticated SSH tunnel requires the
  later Desktop credential and transport integration.
- Code signing, notarization, and auto-update policy are separate release
  hardening tasks.
- The `.dmg` build is verified on the GitHub Actions macOS runner; Linux CI only
  validates the config, scripts, release artifact workflow wiring, Rust host
  tests, and Vite build.
- The release artifact validator checks `.dmg` presence in the artifact list,
  not the binary contents of the disk image.

## Verification

Focused validation:

```bash
PYTHONPATH=src:. python -m pytest tests/ci/test_check_openevo_release.py -q
cd desktop && npm run build:openevo
python desktop/packaging/build_sidecar.py
cd desktop/src-tauri && cargo metadata --locked --format-version 1 && cargo test --locked
git diff --check
```

GitHub Actions verifies the `.dmg` packaging path on `macos-latest` using Node
20, Python 3.11, Rust stable, PyInstaller, `npm ci`, and
`npm run build:desktop`.
