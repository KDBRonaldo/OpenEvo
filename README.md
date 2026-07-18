# OpenEvo

OpenEvo runs scientific tasks through a real agent harness and evolves selected
textual context between sessions. It has exactly two user-facing applications:

- **OpenEvo Desktop Client** is the macOS application used to connect a remote
  server, configure projects, run tasks, and inspect evolution.
- **OpenEvo Daemon** runs under the user's account on the remote Linux server
  and owns execution, transcript capture, projects, artifacts, revisions, and
  managed services.

`src/openevo/` contains the Core implementation assembled into OpenEvo Daemon.
Core is not a third product that users install or operate.

## Install The Preview

Ordinary users should download the macOS DMG from
[GitHub Releases](https://github.com/CompLifeLab-ZJU/OpenEvo/releases) and start
with the [Preview user guide](docs/user/README.md). Do not install OpenEvo from
PyPI or use a repository checkout as the Desktop installation.

The first Preview DMG is unsigned and not notarized. After verifying the release
checksum, macOS users must allow it manually in **System Settings > Privacy &
Security**.

## Preview Scope

The first Preview supports:

- OpenEvo Desktop Client on the release-listed macOS builds;
- OpenEvo Daemon on the release-listed remote Linux x86-64 hosts;
- SSH agent authentication with explicit host-key confirmation;
- Codex subscription execution with transcript capture;
- cross-session textual memory, skill bundle, and agent-system evolution.

The remote SSH user must already have the supported Codex CLI installed and
signed in to a subscription. Desktop checks this during project activation and
returns typed remediation when it is not ready. Desktop uploads and installs
the version-matched Daemon and controlled science runtime; users do not upload a
runtime image.

Self-Deployed execution, parameter or adapter evolution, other harnesses,
in-session evolution, a public CLI, and PyPI distribution are not supported in
this Preview.

## User Workflow

```text
install the unsigned Desktop DMG
-> add a remote server and SSH user
-> verify the SSH host fingerprint
-> let Desktop install or attach OpenEvo Daemon and its managed runtime
-> create and activate a Codex subscription project
-> select textual memory, skills, and/or agent-system evolution
-> run session N and wait for its complete successor revision
-> run session N+1 with the committed evolved context
```

Evolution never changes the session that produced it. All selected outputs from
session N become visible to session N+1 only after the successor revision is
committed. OpenEvo does not silently run against stale or partial context.

Closing Desktop does not stop a remote run. Reopen it, restore access to the
Mac's SSH agent if needed, and reconnect to recover remote state.

## User Documentation

- [Preview overview](docs/user/README.md)
- [Desktop quickstart](docs/user/desktop-quickstart.md)
- [Remote server setup](docs/user/remote-server-setup.md)
- [Proxy and network settings](docs/user/proxy-and-network.md)
- [Troubleshooting](docs/user/troubleshooting.md)
- [Security policy](SECURITY.md)

## Contributor Notes

The Python package and command entrypoints are implementation, Daemon,
maintenance, and CI surfaces. They are not ordinary-user installation methods.

Repository layout:

```text
src/openevo/   Core implementation and OpenEvo Daemon backend
desktop/       OpenEvo Desktop Client, native host, and private sidecar
docs/          user, architecture, Core, and maintainer documentation
tests/         Python and contract regression tests
```

The product dependency direction is
`Desktop Client -> versioned Daemon contracts -> Core implementation`.

### Daemon Maintainer Entrypoints

<!-- openevo:maintainer-only-command -->
```bash {.openevo-maintainer-only}
openevo-backend --help
openevo-backend serve --help
openevo-backend run --help
```

`openevo-backend serve` starts the typed Daemon API used through Desktop's
private SSH tunnel. `openevo-backend run` is maintenance automation, not a user
CLI.

### Development Setup

<!-- openevo:maintainer-only-command -->
```bash {.openevo-maintainer-only}
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest pytest-asyncio ruff build
```

Focused Python checks:

<!-- openevo:maintainer-only-command -->
```bash {.openevo-maintainer-only}
ruff check src tests scripts
PYTHONPATH=src:. python -m pytest tests/ci tests/openevo tests/evolution -q
```

Desktop checks:

<!-- openevo:maintainer-only-command -->
```bash {.openevo-maintainer-only}
cd desktop
npm ci
npm audit --audit-level=high
npm test -- --run
npm run typecheck
npm run build:openevo
cd src-tauri
cargo metadata --locked --format-version 1
cargo test --locked
```

These commands validate a source checkout. They do not publish or install an
ordinary-user release. Release candidates use an exact DMG, Daemon bundle,
managed runtime, manifest, checksums, and release evidence; PyPI is not a
Preview release surface.

## Pre-External-Beta Release Smoke

This maintainer-only gate verifies the exact Desktop DMG, bundled Daemon,
managed runtime, checksums, and evidence before publication. The GitHub Release
is the only Preview distribution surface; it does not publish OpenEvo to PyPI.

The current build exposes a narrow Subscription path for development, marks
Self-Deployed unavailable, and is released as an unsigned Preview. The
canonical External Beta requires both modes and must satisfy the complete
acceptance contract in `docs/maintainer/productization/spec.md`.

## Architecture And Contributing

- Release contract: `docs/maintainer/productization/spec.md`
- Architecture index: `docs/architecture/README.md`
- Daemon API: `docs/core/backend-api.md`
- Repository workflow: `AGENTS.md`
- Contribution guide: `CONTRIBUTING.md`

OpenEvo is in Preview. Non-trivial changes should start from a GitHub issue,
preserve the release boundary, update affected documentation, and pass focused
tests before review.
