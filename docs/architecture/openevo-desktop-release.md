# OpenEvo Desktop Release Packaging

## What Changed

OpenEvo Desktop has a minimal Tauri packaging path for the ordinary-user macOS
release artifact. The release artifact workflow now produces two artifact
classes:

- `openevo-wheel`: the exact `openevo-<version>-*.whl` used for remote install.
- `openevo-desktop-dmg`: a macOS `.dmg` built from the Tauri desktop shell.

The browser shell remains the Dev Kit and debug aid. The `.dmg` is the
ordinary-user release format for macOS.

## Why

The wheel is still the installable Python/OpenEvo runtime artifact, but it is
not an ordinary desktop application package. Tauri provides a thin native shell
around the existing Vite-built Desktop UI so macOS users can install and launch
OpenEvo Desktop as an application bundle distributed in a `.dmg`.

## Consumers

- Release automation consumes `web/package.json` scripts and
  `web/src-tauri/tauri.conf.json` to build the `.dmg`.
- Release validators consume the complete artifact list and require both the
  exact OpenEvo wheel and at least one `.dmg`.
- Ordinary macOS users consume the `.dmg`.
- Developers and CI smoke tests continue to consume the browser-served Desktop
  shell and wheel validation paths.

## Input Contract

The desktop packaging path expects:

- Node dependencies installed with `npm ci` in `web/`.
- Rust stable toolchain available on the macOS runner.
- Rust dependencies locked by `web/src-tauri/Cargo.lock`; CI runs
  `cargo metadata --locked --format-version 1` before packaging.
- Vite output produced by `npm run build`.
- Tauri config at `web/src-tauri/tauri.conf.json` with:
  - `productName`: `OpenEvo Desktop`
  - `identifier`: `org.openevo.desktop`
  - `frontendDist`: `../dist`
  - bundle target: `dmg`
  - macOS `minimumSystemVersion`: `12.0`

## Output Contract

`npm run build:desktop` runs the web build and then `tauri build`. On GitHub
Actions macOS runners, the release workflow uploads:

```text
web/src-tauri/target/release/bundle/dmg/*.dmg
```

The release artifact validator accepts wheel paths for wheel-content checks and
an optional complete artifact list for release-list checks. The complete list
must include:

- `openevo-<version>-*.whl`
- at least one `*.dmg`

Wheel validation remains scoped to `.whl` files only.

## Limitations

- The Tauri shell is intentionally minimal and does not add native sidecar
  supervision or platform-specific update logic.
- The `.dmg` build is verified on the GitHub Actions macOS runner; Linux CI only
  validates the config, scripts, release artifact workflow wiring, and browser
  web build.
- The release artifact validator checks `.dmg` presence in the artifact list,
  not the binary contents of the disk image.

## Verification

Focused validation:

```bash
PYTHONPATH=src:. python -m pytest tests/ci/test_check_openevo_release.py -q
cd web && npm run build
cd web/src-tauri && cargo metadata --locked --format-version 1
git diff --check
```

GitHub Actions verifies the `.dmg` packaging path on `macos-latest` using Node
20, Rust stable, `npm ci`, and `npm run build:desktop`.
