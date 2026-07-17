# OpenEvo

OpenEvo is a product for running and evolving agent systems on real scientific
workflows. It has two release-facing surfaces:

- **OpenEvo Desktop**: the macOS application used by scientists to configure a
  remote server, start runs, and inspect memory, skill, and agent-system
  evolution.
- **OpenEvo Daemon**: the application installed under the user's account on the
  remote Linux server. It owns harness execution, runtime sessions, trajectory
  capture, datasets, jobs, workers, artifacts, revisions, model services, and
  the versioned control API.

`src/openevo/` contains the Core implementation assembled into the Daemon. Core
is not a third release-facing application.

## User Model

The ordinary-user flow is:

```text
Install OpenEvo Desktop .dmg
-> configure a remote server, SSH identity, and optional remote network proxy
-> run doctor/bootstrap from Desktop
-> install or attach the remote OpenEvo Daemon
-> create a science project
-> choose an execution mode currently enabled by the Desktop release
-> launch a science run
-> monitor services, logs, timeline, and evolved artifacts
-> wait for or resolve the atomic successor Project Head
-> run the next task with the committed workspace and evolved context
```

OpenEvo Desktop should not require a scientist to install Python packages,
manually upload runtime images, or operate individual backend services. When a
fresh remote server can be prepared safely from user-level permissions, OpenEvo
does that setup. If Docker permissions, account login, system packages, or
network policy need manual action, Desktop reports a typed error and the next
action.

## Execution Modes

**Codex subscription transcript mode** uses a subscription-authenticated Codex
harness on the remote server. It requires transcript capture, does not call
model APIs directly, and supports non-parametric evolution such as text memory,
skill bundles, and agent-system instructions.

**Self-Deployed mode** uses a manifest-bound Hugging Face model profile served
by Daemon-managed vLLM and still executes tasks through remote Codex.

The canonical External Beta requires both modes. This repository is pre-release:
the current build exposes a narrow Subscription path for development, marks
Self-Deployed unavailable, and has no release-ready candidate. Those
implementation gaps do not redefine the target product.

For every enabled mode, OpenEvo is a wrapper around an existing harness. OpenEvo
captures or ingests trajectories/transcripts, evolves typed artifacts, and
injects the selected context into later sessions.

## Repository Structure

```text
src/openevo/
  backend/       remote Daemon control API and launchers
  deployment/    SSH, preflight, bootstrap, workspace sync, services
  experiments/   experiment compiler and runner
  evolution/     datasets, jobs, workers, artifacts, methods, context resolver
  gateway/       proxy, runtime injection, completion capture
  harness/       Codex and other harness contracts/presets
  projects/      science project schemas and compiler
  rollout/       rollout scheduler and aggregation
  runtime/       Docker and Apptainer runtime abstractions
  trajectory/    trajectory builders and evaluators

desktop/
  src/           React user interface
  src-tauri/     macOS native host
  sidecar/       local Desktop facade
  packaging/     bundled sidecar and packaged web assets

benchmarks/
  terminal_bench/  standalone release-maintainer benchmark automation

docs/
  user/          ordinary-user Desktop guidance
  core/          Daemon and Core contracts
  architecture/  current architecture notes
  maintainer/    release, process, and migration material

examples/
  science-minimal/
  science-with-local-folder/
  self-deployed-model/
  backend-automation/
  research-benchmarks/
```

The product dependency direction is `Desktop -> versioned Daemon contracts` and
`Daemon -> Core implementation`; Core must not import Desktop code. Standalone
benchmark packages may use exact-version Core or versioned Daemon contracts,
while Core and Desktop must not import or package benchmark code.

## Daemon Maintainer Entrypoints

The Core Python package is named `openevo`. Its command entrypoint is a
server-side Daemon launcher and maintainer tool, not an ordinary-user CLI
product:

```bash
openevo-backend --help
openevo-backend serve --help
```

`openevo-backend serve` starts the typed Daemon API used by Desktop through an
SSH tunnel. `openevo-backend run` is a maintenance and automation
entrypoint for experiment snapshots; it is not the ordinary-user product UI.

Maintainer-only backend automation smoke:

<!-- openevo:maintainer-only-command -->
```bash {.openevo-maintainer-only}
openevo-backend run --help
openevo-backend run examples/science-minimal/experiment.yaml --dry-run --json
```

## Development Setup

<!-- openevo:maintainer-only-command -->
```bash {.openevo-maintainer-only}
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install -e benchmarks/terminal_bench
pip install pytest pytest-asyncio ruff build twine
```

Focused Python checks:

<!-- openevo:maintainer-only-command -->
```bash {.openevo-maintainer-only}
ruff check src tests scripts benchmarks/terminal_bench
PYTHONPATH=src:. python -m pytest tests/ci tests/openevo tests/evolution -q
python -m pytest benchmarks/terminal_bench/tests -q
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
# Ubuntu/Linux CI runners need Tauri native packages such as
# libwebkit2gtk-4.1-dev, libayatana-appindicator3-dev, libgtk-3-dev,
# libxdo-dev, librsvg2-dev, and patchelf.
cargo metadata --locked --format-version 1
cargo test --locked
```

Current pull-request release smokes are pre-External-Beta maintainer checks.
They validate the Core wheel identity and Desktop packaging without publishing.
The separately dispatched packaging rehearsal may upload an unsigned draft
prerelease, but its interim wheel and DMG are not a release-ready candidate.
The External Beta release gates and required Daemon Bundle, DMG, checksum, and
release-note artifacts are defined in `docs/maintainer/productization/spec.md`.

## Pre-External-Beta Release Smoke

The historical smoke path is maintainer-only and predates the External Beta
release contract. It is useful for local regression checks, but it is not the
release process and does not create a releasable DMG, GitHub Release, or PyPI
artifact.

External Beta release work must use the manifest-matched Daemon Bundle, the
exact packaged DMG and checksums, and the release gates in
`docs/maintainer/productization/spec.md`. PyPI is not part of this release.

## Examples

- `examples/science-minimal/`: smallest Core experiment config.
- `examples/science-with-local-folder/`: ordinary science project shape that
  uses a user workspace folder.
- `examples/self-deployed-model/`: Self-Deployed Reference model-serving
  configuration notes.
- `examples/backend-automation/`: backend automation examples for maintainers.
- `examples/research-benchmarks/`: release-excluded research/maintainer-only
  benchmark and training examples for OpenEvo developers and researchers, not an
  ordinary-user Desktop quickstart.

Benchmark examples are not Desktop quickstarts. They translate external
benchmark tasks and results into the same Core records, datasets, metrics, jobs,
artifacts, and context inputs that Desktop-backed runs use.

## Documentation

- User guidance: [docs/user/README.md](docs/user/README.md)
- Daemon/Core API: `docs/core/backend-api.md`
- Release-facing architecture index: `docs/architecture/README.md`
- Security policy: [SECURITY.md](SECURITY.md)
- Runtime and evolution contracts:
  `docs/architecture/core-runtime-system-overview.md`,
  `docs/architecture/evolution-api-and-method-integration.md`,
  `docs/architecture/evolution-backend.md`,
  `docs/architecture/evolution-runtime-context.md`,
  `docs/architecture/runtime-injection.md`

## Contributing

OpenEvo is still pre-release. Non-trivial changes should start from a GitHub
issue, preserve existing evolution algorithm behavior unless explicitly scoped
otherwise, update the relevant docs, and pass focused tests before review.
See `AGENTS.md` and `CONTRIBUTING.md` for repository workflow.
