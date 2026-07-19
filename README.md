# OpenEvo

OpenEvo runs scientific tasks through a real agent harness and evolves selected
textual context between sessions. Version `0.1.2` is the ordinary-user
exhibition Preview candidate being prepared for publication; `0.1.1` remains
the current public historical Preview until the `0.1.2` GitHub Release is
published. OpenEvo has exactly two user-facing applications:

- **OpenEvo Desktop Client** is the macOS application used to connect a remote
  server, configure projects, run tasks, and inspect evolution.
- **OpenEvo Daemon** runs under the user's account on the remote Linux server
  and owns execution, transcript capture, projects, artifacts, revisions, and
  managed services.

`src/openevo/` contains the Core implementation assembled into OpenEvo Daemon.
Core is not a third product that users install or operate.

## Install The Preview

After the `0.1.2` Preview is published, ordinary users should download its exact
DMG and `SHA256SUMS` from the same immutable
[GitHub Release](https://github.com/CompLifeLab-ZJU/OpenEvo/releases), then start
with the [Preview user guide](docs/user/README.md). Until that release appears,
the files are not public installation artifacts. Do not install OpenEvo from
PyPI or use a repository checkout as the Desktop installation.

The first Preview DMG is unsigned and not notarized. After verifying the release
checksum, follow the [Desktop quickstart](docs/user/desktop-quickstart.md) to
clear quarantine from the exact installed app or use macOS **Open Anyway**.

## Preview Scope

The `0.1.2` Preview composition packages:

- one Apple Silicon macOS 12+ Desktop DMG;
- the matching Linux x86-64 OpenEvo Daemon Bundle and managed runtime;
- SSH agent authentication with explicit host-key confirmation;
- the intended Codex subscription transcript path and the three textual
  evolution targets.

Its publication workflow requires candidate-bound browser, mounted-DMG,
detached-copy, Daemon Bundle, managed-runtime, checksum, and asset-roundtrip
verification. It does not claim a general clean-host matrix or the canonical
two-session science gate. Treat it as an exhibition artifact, not a generally
supported release. The remote SSH user must already have Codex CLI installed
and signed in to a subscription and must match the documented Docker
user-container assumptions. Desktop uploads the version-matched Daemon Bundle
and controlled science runtime; users do not upload a runtime image.

Self-Deployed execution, parameter or adapter evolution, other harnesses,
in-session evolution, a public CLI, and PyPI distribution are not supported in
this Preview.

The `0.1.2` Preview candidate proves only its real DMG, packaged sidecar and
renderer, Daemon Bundle, and managed-runtime packaging smoke. It is unsigned
and non-gating: it does not prove the canonical science E2E, G2 clean-user
lifecycle, G3 clean-host/network matrix, G12 publication gate, or full External
Beta readiness. The historical `0.1.1` Preview predates the current immutable
Preview publication policy.

## Target User Workflow

```text
install the unsigned Desktop DMG
-> inspect the built-in read-only scientific project tour
-> add a remote server and SSH user
-> verify the SSH host fingerprint
-> let Desktop install or attach OpenEvo Daemon and its managed runtime
-> create and activate a Codex subscription project
-> select textual memory, skills, and/or agent-system evolution
-> run a session and inspect its transcript and textual artifacts
-> run a later session after the evolved context is ready
```

The canonical product contract requires cross-session-only evolution and atomic
successor state. The current v1 Preview does not yet prove that complete
authority contract; the required v2 cutover remains release-blocking.

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
benchmarks/    standalone maintainer benchmark automation; not shipped by either app
docs/          user, architecture, Core, and maintainer documentation
tests/         Python and contract regression tests
```

The product dependency direction is
`Desktop Client -> versioned Daemon contracts -> Core implementation`.
`benchmarks/` may import and exercise Core capabilities, but Core and Desktop
must not import or package benchmark-specific automation.

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

## Preview And External Beta

Preview publication preserves the exact source, tag, Desktop DMG,
self-contained Daemon Bundle, assets, and checksums that passed its real
packaging smoke. The GitHub Release is the only Preview distribution surface;
it does not publish OpenEvo to PyPI.

The current build exposes a narrow Subscription path for development, marks
Self-Deployed unavailable, and is released as an unsigned, non-gating Preview.
That Preview is not reused as a release candidate. The canonical External Beta
requires both modes and must satisfy the complete acceptance contract in
`docs/maintainer/productization/spec.md` against a new immutable candidate.

## Architecture And Contributing

- Release contract: `docs/maintainer/productization/spec.md`
- Architecture index: `docs/architecture/README.md`
- Daemon API: `docs/core/backend-api.md`
- Repository workflow: `AGENTS.md`
- Contribution guide: `CONTRIBUTING.md`

OpenEvo is in Preview. Non-trivial changes should start from a GitHub issue,
preserve the release boundary, update affected documentation, and pass focused
tests before review.
